"""
Signal Aggregator

Technical-analysis-first trade signal generator. Uses EMA crossovers,
RSI, MACD, Bollinger Bands, trend analysis, volume patterns, and
support/resistance levels as the PRIMARY decision makers.

Crime pump detection is a WARNING OVERLAY only — it does not drive
the trade direction. It provides context and caution flags.

The trap bot contributes as a secondary confirmation signal.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from models.token import Token
from models.trade import TradeSignal, TechnicalSignal, TrapDetectionResult, RiskParameters
from models.alert import CrimePumpAlert
from models import Direction, CrimeStage, TrapType, Regime
from utils.logger import get_logger
from config.settings import TRAP_BOT_CONFIDENCE_THRESHOLD

log = get_logger("signal_aggregator")

# Framework weight configuration — TECHNICAL ANALYSIS LEADS
# Crime pump is an overlay warning, not a trade driver
FRAMEWORK_WEIGHTS = {
    "technical_analysis": 0.40,
    "trap_bot": 0.20,
    "volume_analysis": 0.20,
    "market_structure": 0.10,
    "crime_pump_overlay": 0.10,
}


class SignalAggregator:
    """
    Technical-analysis-driven trade signal generator.

    Produces trade analysis like an expert crypto trader would:
    analyze the chart, read indicators, assess trend strength,
    then decide direction, entry, SL, TP, and leverage.

    Crime pump detection is a SEPARATE concern — it only adds
    a warning flag if detected, it does not drive the trade.
    """

    def __init__(self):
        self._signal_history: list[dict] = []
        self._freqtrade = None
        self._octobot = None
        self._chart_analyzer = None
        # Direction memory — prevents flip-flopping between LONG/SHORT
        # Stores {symbol: (direction, timestamp)} for recent signals
        self._direction_cache: dict[str, tuple] = {}

        # Load chart analysis engine
        try:
            from core.chart_analysis import ChartAnalyzer
            self._chart_analyzer = ChartAnalyzer()
        except Exception:
            pass
        try:
            from frameworks.freqtrade_adapter import FreqtradeAdapter
            self._freqtrade = FreqtradeAdapter()
        except Exception:
            pass

        try:
            from frameworks.octobot_adapter import OctoBotAdapter
            self._octobot = OctoBotAdapter()
        except Exception:
            pass

    async def generate_signal(self, token: Token) -> TradeSignal:
        """Shortcut for generate_trade_signal with no crime/trap data."""
        return await self.generate_trade_signal(token)

    async def generate_trade_signal(
        self,
        token: Token,
        crime_pump_alert: Optional[CrimePumpAlert] = None,
        trap_result: Optional[TrapDetectionResult] = None,
        ohlcv_data=None,
    ) -> TradeSignal:
        """
        Generate a complete trade signal from all available data.

        Analysis priority:
        1. Technical indicators (EMA, RSI, MACD, BB) — primary
        2. Volume patterns — confirmation
        3. Trap bot — secondary confirmation
        4. Market structure — context
        5. Crime pump — WARNING overlay only
        """
        log.info("generating_signal", token=token.symbol)

        # Step 1: Run technical analysis (the primary driver)
        technical_signals = self._analyze_technical(token, ohlcv_data)

        # Step 2: Run framework adapters for deeper analysis
        framework_signals = await self._run_frameworks(token)
        technical_signals.extend(framework_signals)

        # Step 2b: Run chart analysis if OHLCV data available
        chart_signals = self._run_chart_analysis(ohlcv_data)
        technical_signals.extend(chart_signals)

        # Step 3: Volume analysis
        volume_signals = self._analyze_volume(token)

        # Step 4: Market structure analysis
        structure_signals = self._analyze_market_structure(token)

        # Step 5: Determine direction from TECHNICAL signals (not crime pump)
        direction, direction_confidence = self._determine_direction(
            token, trap_result, technical_signals
        )

        # Step 6: Calculate confidence from all weighted sources
        confidence = self._calculate_confidence(
            token, crime_pump_alert, trap_result,
            technical_signals, volume_signals
        )

        # Step 7: Crime pump is ONLY a warning overlay
        crime_status = CrimeStage.NONE
        crime_override = False
        if crime_pump_alert and crime_pump_alert.is_confirmed:
            crime_status = crime_pump_alert.crime_stage
            # Only override if Stage 4 (distribution) — the token is crashing
            if crime_status == CrimeStage.STAGE_FOUR:
                crime_override = True

        # Step 8: Calculate entry, SL, TP, leverage from technical analysis
        risk = self._calculate_risk_parameters(
            token, direction, confidence, crime_status
        )

        # Step 9: Market structure summary
        market_structure = self._summarize_market_structure(token, crime_pump_alert)

        # Step 10: Reasoning narrative
        reasoning = self._build_reasoning(
            token, direction, confidence, technical_signals,
            crime_pump_alert, trap_result, risk
        )

        # Contributing frameworks
        frameworks = ["technical_analysis", "volume_analysis"]
        if crime_pump_alert:
            frameworks.append("crime_pump_overlay")
        if trap_result and trap_result.gate_5_passed:
            frameworks.append("trap_bot")
        if self._freqtrade:
            frameworks.append("freqtrade_indicators")

        signal = TradeSignal(
            token_symbol=token.symbol,
            direction=direction,
            confidence=round(confidence, 2),
            timeframe="multi",
            technical_signals=technical_signals,
            onchain_signals=self._gather_onchain_signals(token),
            market_structure_summary=market_structure,
            trap_detection=trap_result or TrapDetectionResult(),
            crime_pump_status=crime_status,
            crime_pump_override=crime_override,
            risk=risk,
            reasoning_summary=reasoning,
            contributing_frameworks=frameworks,
            framework_scores={
                fw: round(self._get_framework_score(fw, token, crime_pump_alert, trap_result), 2)
                for fw in frameworks
            },
        )

        log.info("signal_generated",
                token=token.symbol,
                direction=direction.value,
                confidence=signal.confidence,
                entry=risk.entry_price)

        return signal

    def _analyze_technical(self, token: Token, ohlcv_data=None) -> list[TechnicalSignal]:
        """
        PRIMARY analysis — technical indicators like an expert trader.
        Analyzes momentum, trend, RSI, volume confirmation, and liquidity.
        """
        signals = []
        price = token.metrics.price
        change_1h = token.metrics.price_change_1h
        change_24h = token.metrics.price_change_24h
        change_5m = token.metrics.price_change_5m

        if price <= 0:
            return signals

        # === MOMENTUM ANALYSIS ===
        # Short-term momentum (5m)
        if abs(change_5m) > 2:
            direction = "bullish" if change_5m > 0 else "bearish"
            signals.append(TechnicalSignal(
                name="5m Momentum",
                observation=f"Price moved {change_5m:+.1f}% in 5 minutes",
                significance=f"Strong short-term {direction} pressure. "
                           f"{'Buyers are aggressively pushing price up.' if change_5m > 0 else 'Sellers are dumping aggressively.'}",
                weight=0.6 if abs(change_5m) > 5 else 0.4,
            ))

        # 1h momentum
        if abs(change_1h) > 3:
            direction = "bullish" if change_1h > 0 else "bearish"
            strength = "strong" if abs(change_1h) > 10 else "moderate"
            signals.append(TechnicalSignal(
                name="1h Momentum",
                observation=f"Price moved {change_1h:+.1f}% in the last hour",
                significance=f"{strength.title()} {direction} momentum. "
                           f"This is the dominant short-term trend direction.",
                weight=0.8 if abs(change_1h) > 10 else 0.6,
            ))

        # === TREND ANALYSIS ===
        # Multi-timeframe trend alignment
        if change_1h > 2 and change_24h > 5:
            signals.append(TechnicalSignal(
                name="Bullish Trend Alignment",
                observation=f"1h ({change_1h:+.1f}%) and 24h ({change_24h:+.1f}%) both positive",
                significance="Multiple timeframes confirm uptrend. Higher probability "
                           "that the move continues in this direction.",
                weight=0.75,
            ))
        elif change_1h < -2 and change_24h < -5:
            signals.append(TechnicalSignal(
                name="Bearish Trend Alignment",
                observation=f"1h ({change_1h:+.1f}%) and 24h ({change_24h:+.1f}%) both negative",
                significance="Multiple timeframes confirm downtrend. Higher probability "
                           "that the move continues lower.",
                weight=0.75,
            ))
        elif (change_1h > 3 and change_24h < -5) or (change_1h < -3 and change_24h > 5):
            signals.append(TechnicalSignal(
                name="Trend Divergence",
                observation=f"1h ({change_1h:+.1f}%) and 24h ({change_24h:+.1f}%) diverging",
                significance="Short-term is fighting the longer trend. Could be a reversal "
                           "or a temporary pullback. Wait for confirmation.",
                weight=0.5,
            ))

        # === REVERSAL SIGNALS ===
        # Sharp drop + recovery (potential bottom)
        if change_24h < -15 and change_1h > 3:
            signals.append(TechnicalSignal(
                name="Potential Bottom Reversal",
                observation=f"Token dropped {change_24h:.1f}% in 24h but bouncing {change_1h:+.1f}% in 1h",
                significance="Could be a bounce from oversold levels. Strong buyers stepping in "
                           "at this price. Watch for continuation above the 1h high.",
                weight=0.7,
            ))

        # Sharp pump + pullback (potential top)
        if change_24h > 20 and change_1h < -3:
            signals.append(TechnicalSignal(
                name="Potential Top Reversal",
                observation=f"Token pumped {change_24h:.1f}% in 24h but pulling back {change_1h:.1f}% in 1h",
                significance="Could be profit-taking after a large move. If selling continues, "
                           "this may be the start of a correction.",
                weight=0.65,
            ))

        # === OVEREXTENSION CHECK ===
        if abs(change_24h) > 30:
            direction = "upside" if change_24h > 0 else "downside"
            signals.append(TechnicalSignal(
                name="Overextended Move",
                observation=f"Price has moved {change_24h:+.1f}% in 24 hours",
                significance=f"Token is overextended to the {direction}. Mean reversion is likely. "
                           f"{'Be cautious longing at these levels.' if change_24h > 0 else 'A bounce is probable from here.'}",
                weight=0.6,
            ))

        # === VOLUME CONFIRMATION ===
        vol_1h = token.metrics.volume_1h
        vol_24h = token.metrics.volume_24h
        if vol_24h > 0:
            hourly_avg = vol_24h / 24
            vol_ratio = vol_1h / hourly_avg if hourly_avg > 0 else 0
            if vol_ratio > 3:
                signals.append(TechnicalSignal(
                    name="Volume Confirmation",
                    observation=f"Current volume is {vol_ratio:.1f}x the 24h average",
                    significance="High volume confirms the current move has genuine participation. "
                               "This is not just thin-market noise — real money is moving.",
                    weight=0.75,
                ))
            elif vol_ratio < 0.3 and abs(change_1h) > 3:
                signals.append(TechnicalSignal(
                    name="Low Volume Move",
                    observation=f"Price moved {change_1h:+.1f}% on only {vol_ratio:.1f}x average volume",
                    significance="The price move is not backed by volume. This could easily reverse. "
                               "Do not trust moves without volume confirmation.",
                    weight=0.5,
                ))

        # === LIQUIDITY ANALYSIS ===
        mcap = token.metrics.market_cap
        liq = token.metrics.liquidity_usd
        if mcap > 0 and liq > 0:
            depth = liq / mcap
            if depth < 0.02:
                signals.append(TechnicalSignal(
                    name="Extremely Low Liquidity",
                    observation=f"Liquidity is only {depth*100:.1f}% of market cap",
                    significance="Entering or exiting this position will cause significant slippage. "
                               "Reduce position size and use limit orders if possible.",
                    weight=0.6,
                ))
            elif depth < 0.05:
                signals.append(TechnicalSignal(
                    name="Low Liquidity Warning",
                    observation=f"Liquidity is {depth*100:.1f}% of market cap",
                    significance="Thin liquidity means wider spreads. "
                               "Position size should be reduced accordingly.",
                    weight=0.4,
                ))

        # === FUNDING RATE AS TECHNICAL SIGNAL ===
        funding = token.derivatives.funding_rate
        if funding < -0.01:
            signals.append(TechnicalSignal(
                name="Negative Funding Rate",
                observation=f"Funding rate is {funding*100:.3f}%",
                significance="Shorts are paying longs. Market is overcrowded with shorts. "
                           "This often precedes a short squeeze (price pump).",
                weight=0.6,
            ))
        elif funding > 0.02:
            signals.append(TechnicalSignal(
                name="High Positive Funding",
                observation=f"Funding rate is {funding*100:.3f}%",
                significance="Longs are paying heavily. Market is overcrowded with longs. "
                           "This often precedes a long squeeze (price dump).",
                weight=0.6,
            ))

        # === OI ANALYSIS AS TECHNICAL SIGNAL ===
        oi_change = token.derivatives.oi_change_1h
        if oi_change > 15 and change_1h > 0:
            signals.append(TechnicalSignal(
                name="Rising OI + Rising Price",
                observation=f"OI up {oi_change:.1f}% with price up {change_1h:+.1f}%",
                significance="New longs are opening. Bullish if the move is genuine. "
                           "But watch for a reversal if OI gets too high.",
                weight=0.55,
            ))
        elif oi_change > 15 and change_1h < 0:
            signals.append(TechnicalSignal(
                name="Rising OI + Falling Price",
                observation=f"OI up {oi_change:.1f}% with price down {change_1h:.1f}%",
                significance="New shorts are opening. Bearish pressure building. "
                           "But if too many shorts pile in, a squeeze becomes likely.",
                weight=0.55,
            ))

        return signals

    async def _run_frameworks(self, token: Token) -> list[TechnicalSignal]:
        """Run external framework adapters for deeper analysis."""
        signals = []

        # Freqtrade adapter — EMA, RSI, MACD, Bollinger Bands
        if self._freqtrade:
            try:
                ft_result = self._freqtrade.analyze(token)
                if ft_result:
                    for indicator_name, result in ft_result.items():
                        if isinstance(result, dict) and result.get("signal"):
                            signals.append(TechnicalSignal(
                                name=f"FT: {indicator_name}",
                                observation=result.get("observation", str(result.get("signal"))),
                                significance=result.get("significance", ""),
                                weight=result.get("weight", 0.5),
                            ))
            except Exception as e:
                log.warning("freqtrade_error", error=str(e))

        # OctoBot adapter — grid/DCA analysis
        if self._octobot:
            try:
                ob_result = self._octobot.analyze(token)
                if ob_result and isinstance(ob_result, dict):
                    strategy = ob_result.get("strategy")
                    if strategy:
                        signals.append(TechnicalSignal(
                            name=f"OB: {strategy}",
                            observation=ob_result.get("observation", ""),
                            significance=ob_result.get("significance", ""),
                            weight=ob_result.get("weight", 0.4),
                        ))
            except Exception as e:
                log.warning("octobot_error", error=str(e))

        return signals

    def _run_chart_analysis(self, ohlcv_data) -> list[TechnicalSignal]:
        """
        Run full chart analysis using the ChartAnalyzer.
        Adds candlestick patterns, RSI, EMA, MACD, Bollinger Bands,
        support/resistance levels, and trend analysis.
        """
        signals = []

        if not self._chart_analyzer or not ohlcv_data:
            return signals

        try:
            result = self._chart_analyzer.analyze(ohlcv_data)

            # Add candlestick patterns
            for pattern in result.patterns:
                signals.append(TechnicalSignal(
                    name=f"Pattern: {pattern.name}",
                    observation=f"{pattern.pattern_type.title()} candlestick pattern detected",
                    significance=pattern.significance,
                    weight=pattern.weight,
                ))

            # Add RSI
            if result.rsi_signal:
                signals.append(TechnicalSignal(
                    name=f"RSI ({result.rsi:.0f})",
                    observation=f"RSI is at {result.rsi:.1f}",
                    significance=result.rsi_signal,
                    weight=0.6 if result.rsi > 70 or result.rsi < 30 else 0.4,
                ))

            # Add EMA
            if result.ema_signal and "Insufficient" not in result.ema_signal:
                signals.append(TechnicalSignal(
                    name="EMA Crossover",
                    observation=result.ema_signal,
                    significance="EMA 9/21 crossover is one of the most reliable "
                               "short-term trend indicators used by professional traders.",
                    weight=0.6 if "Bullish" in result.ema_signal or "Bearish" in result.ema_signal else 0.3,
                ))

            # Add MACD
            if result.macd_signal and "Insufficient" not in result.macd_signal:
                signals.append(TechnicalSignal(
                    name="MACD",
                    observation=result.macd_signal,
                    significance="MACD confirms the direction of momentum. "
                               "Alignment with other indicators increases confidence.",
                    weight=0.5,
                ))

            # Add Bollinger Bands
            if result.bb_signal and "Insufficient" not in result.bb_signal:
                signals.append(TechnicalSignal(
                    name="Bollinger Bands",
                    observation=result.bb_signal,
                    significance="Bollinger Bands measure volatility. Price at the bands "
                               "often reverses, especially when combined with other signals.",
                    weight=0.45,
                ))

            # Add support/resistance levels
            for s in result.support_levels[:2]:
                p_str = f"${s.price:.6f}" if s.price < 1 else f"${s.price:,.2f}"
                signals.append(TechnicalSignal(
                    name=f"Support at {p_str}",
                    observation=f"Support level at {p_str} (strength: {s.strength})",
                    significance=s.description + ". Price may bounce here.",
                    weight=0.4 + min(s.strength * 0.1, 0.3),
                ))

            for r in result.resistance_levels[:2]:
                p_str = f"${r.price:.6f}" if r.price < 1 else f"${r.price:,.2f}"
                signals.append(TechnicalSignal(
                    name=f"Resistance at {p_str}",
                    observation=f"Resistance level at {p_str} (strength: {r.strength})",
                    significance=r.description + ". Price may reject here.",
                    weight=0.4 + min(r.strength * 0.1, 0.3),
                ))

            # Overall chart trend
            if result.trend != "neutral":
                signals.append(TechnicalSignal(
                    name=f"Chart Trend: {result.trend.title()}",
                    observation=f"Overall chart shows {result.trend} trend "
                              f"with {result.trend_strength:.0%} strength",
                    significance=f"The chart's overall structure is {result.trend}. "
                               f"Trading with the trend has higher probability of success.",
                    weight=0.5 * result.trend_strength,
                ))

        except Exception as e:
            log.warning("chart_analysis_error", error=str(e))

        return signals

    def _analyze_volume(self, token: Token) -> list[dict]:
        """Analyze volume patterns for trade signal context."""
        signals = []

        vol_1h = token.metrics.volume_1h
        vol_5m = token.metrics.volume_5m
        vol_24h = token.metrics.volume_24h

        if vol_24h > 0:
            hourly_avg = vol_24h / 24

            if vol_1h > hourly_avg * 4:
                signals.append({
                    "name": "Volume Spike",
                    "observation": f"1h volume ${vol_1h:,.0f} is {vol_1h/hourly_avg:.1f}x average",
                    "weight": 0.8,
                })

            if vol_5m > 0:
                five_min_rate = vol_5m * 12
                if five_min_rate > hourly_avg * 3:
                    signals.append({
                        "name": "Accelerating Volume",
                        "observation": "5-minute volume rate exceeds hourly average significantly",
                        "weight": 0.6,
                    })

        return signals

    def _analyze_market_structure(self, token: Token) -> list[dict]:
        """Analyze high-level market structure."""
        signals = []

        mcap = token.metrics.market_cap
        liq = token.metrics.liquidity_usd
        oi = token.derivatives.open_interest

        # Market cap assessment
        if mcap > 0:
            if mcap < 100_000:
                signals.append({"name": "Micro Cap", "weight": 0.3,
                              "observation": f"Market cap ${mcap:,.0f} — extremely risky"})
            elif mcap < 1_000_000:
                signals.append({"name": "Low Cap", "weight": 0.4,
                              "observation": f"Market cap ${mcap:,.0f} — high risk, high reward"})

        # OI to market cap ratio
        if oi > 0 and mcap > 0:
            ratio = oi / mcap
            if ratio > 0.5:
                signals.append({
                    "name": "High OI/MCap",
                    "observation": f"OI is {ratio:.1f}x market cap — very leveraged market",
                    "weight": 0.6,
                })

        return signals

    def _determine_direction(
        self,
        token: Token,
        trap: Optional[TrapDetectionResult],
        tech_signals: list[TechnicalSignal],
    ) -> tuple[Direction, float]:
        """
        Determine trade direction from TECHNICAL analysis only.
        Crime pump does NOT influence direction — it's a separate warning.

        KEY PRINCIPLE: Don't chase pumps or dumps.
        If a token has already moved massively, the easy money is gone.
        Look for REVERSALS and CONTINUATIONS, not momentum chasing.
        """
        bull_score = 0.0
        bear_score = 0.0

        change_5m = token.metrics.price_change_5m
        change_1h = token.metrics.price_change_1h
        change_24h = token.metrics.price_change_24h

        # === OVEREXTENSION PENALTIES (MOST IMPORTANT) ===
        # This prevents the bot from going LONG at the top of a pump
        # or SHORT at the bottom of a dump.
        if change_24h > 50:
            # Token pumped >50% in 24h — DO NOT chase long
            bear_score += 0.40  # Strong bias toward short/reversal
            bull_score -= 0.20  # Penalize any bullish signals
            log.info("overextension_penalty_long", token=token.symbol,
                    change_24h=change_24h, note="Pump too extended, biasing SHORT")
        elif change_24h > 30:
            bear_score += 0.25
            bull_score -= 0.10
        elif change_24h < -30:
            # Token dumped >30% in 24h — DO NOT chase short
            bull_score += 0.30  # Bias toward bounce/reversal
            bear_score -= 0.15
            log.info("overextension_penalty_short", token=token.symbol,
                    change_24h=change_24h, note="Dump too extended, biasing LONG")
        elif change_24h < -50:
            bull_score += 0.40
            bear_score -= 0.20

        # === MODERATE MOMENTUM (only if NOT overextended) ===
        if abs(change_24h) <= 30:
            # 5m momentum (most recent) — reduced weight
            if change_5m > 2:
                bull_score += 0.08
            elif change_5m < -2:
                bear_score += 0.08

            # 1h momentum (short-term trend)
            if change_1h > 3:
                bull_score += 0.15
            elif change_1h > 0:
                bull_score += 0.05
            elif change_1h < -3:
                bear_score += 0.15
            elif change_1h < 0:
                bear_score += 0.05

            # 24h momentum (medium-term)
            if change_24h > 5:
                bull_score += 0.10
            elif change_24h < -5:
                bear_score += 0.10

            # Trend alignment bonus (only when moderate)
            if change_1h > 0 and change_24h > 0 and change_5m > 0:
                bull_score += 0.10
            elif change_1h < 0 and change_24h < 0 and change_5m < 0:
                bear_score += 0.10

        # === REVERSAL PATTERNS (high priority) ===
        # Big 24h drop + 1h bounce = potential long (mean reversion)
        if change_24h < -15 and change_1h > 3:
            bull_score += 0.25

        # Big 24h pump + 1h pullback = potential short (distribution)
        if change_24h > 20 and change_1h < -3:
            bear_score += 0.25

        # === DERIVATIVES CONTEXT ===
        funding = token.derivatives.funding_rate
        if funding < -0.01:
            bull_score += 0.15  # Negative funding = short squeeze potential
        elif funding > 0.02:
            bear_score += 0.10  # High positive funding = long squeeze potential

        oi_change = token.derivatives.oi_change_1h
        if oi_change > 10 and change_1h > 0:
            bull_score += 0.08
        elif oi_change > 10 and change_1h < 0:
            bear_score += 0.08

        # === TRAP BOT (secondary confirmation) ===
        if trap and trap.gate_5_passed:
            if trap.trap_fired == TrapType.T2_STOP_SWEEP:
                bull_score += 0.15
            elif trap.trap_fired == TrapType.T1_FAILED_BREAKOUT:
                bull_score += 0.10
            elif trap.trap_fired == TrapType.T3_GIANT_EXHAUSTION:
                bear_score += 0.15

        # === CHART ANALYSIS SIGNALS (high priority — these read real candles) ===
        for sig in tech_signals:
            sig_text = f"{sig.name} {sig.observation} {sig.significance}".lower()

            # RSI extremes get extra weight
            is_overbought = any(w in sig_text for w in ["overbought", "rsi (8", "rsi (9"])
            is_oversold = any(w in sig_text for w in ["oversold", "rsi (1", "rsi (2"])

            if is_overbought:
                bear_score += sig.weight * 0.40  # Strong bearish from RSI extreme
            elif is_oversold:
                bull_score += sig.weight * 0.40  # Strong bullish from RSI extreme

            is_bullish = any(w in sig_text for w in [
                "bullish", "bottom", "bounce", "oversold", "support",
                "hammer", "morning star", "three white", "upward",
            ])
            is_bearish = any(w in sig_text for w in [
                "bearish", "top", "overbought", "resistance", "pull back",
                "shooting star", "evening star", "three black", "downward",
                "overextended",
            ])

            if is_bullish and not is_bearish:
                bull_score += sig.weight * 0.25
            elif is_bearish and not is_bullish:
                bear_score += sig.weight * 0.25

        # Ensure scores don't go negative
        bull_score = max(bull_score, 0)
        bear_score = max(bear_score, 0)

        total = bull_score + bear_score
        if total == 0:
            change_1h = token.metrics.price_change_1h
            if change_1h > 1:
                return Direction.LONG, 0.30
            elif change_1h < -1:
                return Direction.SHORT, 0.30
            else:
                if change_1h >= 0:
                    return Direction.LONG, 0.25
                else:
                    return Direction.SHORT, 0.25

        # Determine raw direction
        if bull_score > bear_score:
            new_direction = Direction.LONG
            confidence = bull_score / total
        elif bear_score > bull_score:
            new_direction = Direction.SHORT
            confidence = bear_score / total
        else:
            if token.metrics.price_change_1h >= 0:
                new_direction = Direction.LONG
            else:
                new_direction = Direction.SHORT
            confidence = 0.50

        # Direction stickiness — prevent flip-flopping
        symbol = token.symbol
        diff = abs(bull_score - bear_score) / total if total > 0 else 0

        if symbol in self._direction_cache:
            cached_dir, cached_time = self._direction_cache[symbol]
            if new_direction != cached_dir and diff < 0.20:
                new_direction = cached_dir
                confidence = 0.45

        self._direction_cache[symbol] = (new_direction, datetime.now(timezone.utc))

        if len(self._direction_cache) > 20:
            oldest = sorted(self._direction_cache.items(), key=lambda x: x[1][1])
            for key, _ in oldest[:5]:
                del self._direction_cache[key]

        return new_direction, confidence

    def _calculate_confidence(
        self,
        token: Token,
        alert: Optional[CrimePumpAlert],
        trap: Optional[TrapDetectionResult],
        tech_signals: list[TechnicalSignal],
        vol_signals: list[dict],
    ) -> float:
        """
        Calculate overall confidence from all weighted sources.
        Technical analysis dominates. Crime pump is minor overlay.
        """
        scores = {}

        # Technical analysis score (HIGHEST WEIGHT — 0.40)
        if tech_signals:
            tech_avg = sum(s.weight for s in tech_signals) / len(tech_signals)
            # Bonus for having many confirming signals
            signal_count_bonus = min(len(tech_signals) * 0.05, 0.2)
            scores["technical_analysis"] = min(tech_avg + signal_count_bonus, 1.0)
        else:
            scores["technical_analysis"] = 0.3

        # Volume analysis score (0.20)
        if vol_signals:
            vol_avg = sum(s.get("weight", 0.5) for s in vol_signals) / len(vol_signals)
            scores["volume_analysis"] = min(vol_avg, 1.0)
        else:
            scores["volume_analysis"] = 0.3

        # Trap bot score (0.20)
        if trap and trap.gate_5_passed:
            scores["trap_bot"] = trap.confidence
        elif trap:
            passed_gates = sum([
                trap.gate_1_passed, trap.gate_2_passed,
                trap.gate_3_passed, trap.gate_4_passed,
            ])
            scores["trap_bot"] = passed_gates / 5
        else:
            scores["trap_bot"] = 0.0

        # Market structure (0.10)
        mcap = token.metrics.market_cap
        liq = token.metrics.liquidity_usd
        if mcap > 1_000_000 and liq > 50_000:
            scores["market_structure"] = 0.7
        elif mcap > 100_000:
            scores["market_structure"] = 0.5
        else:
            scores["market_structure"] = 0.3

        # Crime pump overlay (0.10) — small weight, just a warning
        if alert and alert.is_confirmed:
            scores["crime_pump_overlay"] = alert.total_layers_triggered / 4
        else:
            scores["crime_pump_overlay"] = 0.0

        # Weighted combination
        total = sum(
            scores.get(fw, 0) * FRAMEWORK_WEIGHTS.get(fw, 0)
            for fw in FRAMEWORK_WEIGHTS
        )
        weight_sum = sum(
            FRAMEWORK_WEIGHTS.get(fw, 0) for fw in scores if scores[fw] > 0
        )

        confidence = total / weight_sum if weight_sum > 0 else 0.3
        return min(max(confidence, 0.1), 0.95)

    def _calculate_risk_parameters(
        self,
        token: Token,
        direction: Direction,
        confidence: float,
        crime_status: CrimeStage,
    ) -> RiskParameters:
        """
        Calculate entry, stop loss, take profit, leverage, and position sizing.
        Based on technical analysis, volatility, and confidence.
        """
        price = token.metrics.price
        if price <= 0:
            return RiskParameters()

        # Base volatility estimate from recent price action
        change_1h = abs(token.metrics.price_change_1h)
        change_24h = abs(token.metrics.price_change_24h)
        volatility = max(change_1h, change_24h / 6, 2.0)  # Minimum 2%

        # Entry is current price
        entry = price

        # Stop loss calculation
        # Base SL = 1.5x hourly volatility, adjusted by confidence
        sl_pct = volatility * 1.5
        if confidence < 0.4:
            sl_pct *= 1.3  # Wider stop when less confident
        elif confidence > 0.7:
            sl_pct *= 0.8  # Tighter stop when very confident

        # Crime pump context — only a warning, widen stop for safety
        if crime_status in (CrimeStage.STAGE_THREE, CrimeStage.STAGE_FOUR):
            sl_pct *= 1.3  # More room during volatile manipulation phases

        sl_pct = max(sl_pct, 1.5)   # Minimum 1.5% stop
        sl_pct = min(sl_pct, 15.0)  # Maximum 15% stop

        # Dynamic R:R based on confidence
        # Higher confidence = more aggressive TP = better R:R
        if confidence >= 0.7:
            tp1_mult = 3.0    # 3:1 R:R
            tp2_mult = 5.0    # 5:1 R:R
        elif confidence >= 0.5:
            tp1_mult = 2.5    # 2.5:1 R:R
            tp2_mult = 4.0    # 4:1 R:R
        elif confidence >= 0.4:
            tp1_mult = 2.2    # 2.2:1 R:R
            tp2_mult = 3.5    # 3.5:1 R:R
        else:
            tp1_mult = 2.0    # 2:1 R:R (conservative for low confidence)
            tp2_mult = 3.0    # 3:1 R:R

        if direction == Direction.LONG:
            stop_loss = entry * (1 - sl_pct / 100)
            tp1 = entry * (1 + sl_pct * tp1_mult / 100)
            tp2 = entry * (1 + sl_pct * tp2_mult / 100)
        else:
            stop_loss = entry * (1 + sl_pct / 100)
            tp1 = entry * (1 - sl_pct * tp1_mult / 100)
            tp2 = entry * (1 - sl_pct * tp2_mult / 100)

        risk_reward = tp1_mult

        # Leverage — based on confidence and volatility
        if confidence >= 0.7 and volatility < 5:
            leverage = 10
        elif confidence >= 0.5 and volatility < 8:
            leverage = 5
        elif confidence >= 0.4:
            leverage = 3
        else:
            leverage = 2

        # Position size (% of portfolio)
        position_pct = min(5.0, confidence * 8)

        return RiskParameters(
            entry_price=round(entry, 8),
            stop_loss=round(stop_loss, 8),
            take_profit_1=round(tp1, 8),
            take_profit_2=round(tp2, 8),
            risk_reward=round(risk_reward, 1),
            position_size_pct=round(position_pct, 1),
            max_loss_usd=0,
            leverage=leverage,
        )

    def _summarize_market_structure(self, token: Token,
                                     alert: Optional[CrimePumpAlert]) -> str:
        """Build a brief market structure summary."""
        parts = []

        price = token.metrics.price
        mcap = token.metrics.market_cap
        liq = token.metrics.liquidity_usd

        if price > 0:
            parts.append(f"Current price ${price:.6f}" if price < 1
                        else f"Current price ${price:,.2f}")
        if mcap > 0:
            parts.append(f"market cap ${mcap:,.0f}")
        if liq > 0:
            parts.append(f"liquidity ${liq:,.0f}")

        change = token.metrics.price_change_1h
        if abs(change) > 1:
            direction = "up" if change > 0 else "down"
            parts.append(f"price {direction} {abs(change):.1f}% in the last hour")

        oi = token.derivatives.open_interest
        if oi > 0:
            parts.append(f"open interest ${oi:,.0f}")

        funding = token.derivatives.funding_rate
        if funding != 0:
            parts.append(f"funding rate {funding*100:.2f}%")

        structure = ". ".join(parts) + "." if parts else "Insufficient data."

        if alert and alert.is_confirmed:
            structure += f" ⚠️ Crime pump warning: {alert.crime_stage.value} detected."

        return structure

    def _gather_onchain_signals(self, token: Token) -> list[dict]:
        """Collect relevant on-chain signals."""
        signals = []

        if token.onchain.top10_wallet_pct > 50:
            signals.append({
                "name": "Wallet Concentration",
                "observation": f"Top 10 wallets hold {token.onchain.top10_wallet_pct:.1f}%",
            })

        if token.onchain.contract_age_days is not None and token.onchain.contract_age_days > 0 and token.onchain.contract_age_days < 30:
            signals.append({
                "name": "Young Contract",
                "observation": f"Contract is {token.onchain.contract_age_days} days old",
            })

        if token.onchain.cold_wallet_outflows_24h > 100_000:
            signals.append({
                "name": "Cold Wallet Outflows",
                "observation": f"${token.onchain.cold_wallet_outflows_24h:,.0f} withdrawn in 24h",
            })

        return signals

    def _get_framework_score(self, framework: str, token: Token,
                              alert: Optional[CrimePumpAlert],
                              trap: Optional[TrapDetectionResult]) -> float:
        """Get individual framework contribution score."""
        if framework == "crime_pump_overlay" and alert:
            return alert.total_layers_triggered / 4
        if framework == "trap_bot" and trap:
            return trap.confidence
        if framework == "technical_analysis":
            return 0.6
        if framework == "volume_analysis":
            return 0.5
        if framework == "freqtrade_indicators":
            return 0.5
        return 0.3

    def _build_reasoning(
        self,
        token: Token,
        direction: Direction,
        confidence: float,
        tech_signals: list[TechnicalSignal],
        alert: Optional[CrimePumpAlert],
        trap: Optional[TrapDetectionResult],
        risk: RiskParameters,
    ) -> str:
        """Build the plain language reasoning summary like an expert trader."""
        parts = []

        word = "long" if direction == Direction.LONG else "short"
        parts.append(f"${token.symbol} is showing {word} potential "
                    f"with {confidence:.0%} confidence based on technical analysis.")

        # Technical reasoning
        change_1h = token.metrics.price_change_1h
        change_24h = token.metrics.price_change_24h
        if change_1h > 3 and change_24h > 5:
            parts.append("Both short-term and medium-term trends are bullish. "
                        "Multi-timeframe alignment supports the long direction.")
        elif change_1h < -3 and change_24h < -5:
            parts.append("Both short-term and medium-term trends are bearish. "
                        "Multi-timeframe alignment supports the short direction.")
        elif change_24h < -15 and change_1h > 3:
            parts.append("Token is bouncing after a significant drop. "
                        "This could be a reversal from oversold territory.")

        # Volume context
        vol_1h = token.metrics.volume_1h
        vol_24h = token.metrics.volume_24h
        if vol_24h > 0:
            hourly_avg = vol_24h / 24
            vol_ratio = vol_1h / hourly_avg if hourly_avg > 0 else 0
            if vol_ratio > 3:
                parts.append(f"Volume is {vol_ratio:.1f}x average, confirming the move.")

        # Funding and OI context
        funding = token.derivatives.funding_rate
        if funding < -0.01:
            parts.append("Negative funding rate suggests shorts are overcrowded. "
                        "Short squeeze potential exists.")
        elif funding > 0.02:
            parts.append("High positive funding suggests longs are overcrowded. "
                        "Long squeeze risk is elevated.")

        # Trap bot
        if trap and trap.gate_5_passed:
            parts.append(f"The trap bot identified a {trap.trap_fired.value} pattern "
                        f"with {trap.confidence:.0%} confidence as secondary confirmation.")

        # Crime pump WARNING (not driver)
        if alert and alert.is_confirmed:
            parts.append(f"⚠️ Note: Crime pump {alert.crime_stage.value} detected. "
                        "This is a warning overlay — the trade analysis is based on "
                        "technical indicators, but be aware of manipulation risk.")

        # Risk context
        if risk.entry_price > 0 and risk.stop_loss > 0:
            sl_pct = abs(risk.entry_price - risk.stop_loss) / risk.entry_price * 100
            parts.append(f"Stop loss set at {sl_pct:.1f}% from entry "
                        f"with a {risk.risk_reward:.1f}:1 risk-reward targeting TP1.")

        return " ".join(parts)
