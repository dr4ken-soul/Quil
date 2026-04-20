"""
hl-trap-bot: Five-Gate Price Action Trap Detection Pipeline

Implements the complete 5-gate pipeline for detecting price action traps
that map directly to crime pump lifecycle stages:

Gate 1: Regime Classifier - Trend / Range / Unknown
Gate 2: Location Filter - Price location relative to key levels
Gate 3: Trap Detector - T1 through T5 trap pattern recognition
Gate 4: Confirmation Engine - Volume, structure, and momentum confirmation
Gate 5: Edge Model - Final confidence scoring combining all gates

Trap types and their crime pump mappings:
T1 Failed Breakout -> False move to attract early traders
T2 Stop Sweep -> Stage 3 stop-hunt behavior (most critical)
T3 Giant Exhaustion -> Stage 4 distribution signal
T4 Outside Bar Double Trap -> Stage 3 reversal confirmation
T5 First Deep Pullback -> Stage 1-2 accumulation zone identification
"""

import numpy as np
import pandas as pd
from typing import Optional
from dataclasses import dataclass, field
from models import Regime, TrapType
from models.trade import TrapDetectionResult
from utils.logger import get_logger
from config.settings import (
    TRAP_BOT_LOOKBACK_BARS,
    TRAP_BOT_EMA_FAST,
    TRAP_BOT_EMA_SLOW,
    TRAP_BOT_ATR_PERIOD,
    TRAP_BOT_VOLUME_SPIKE_MULTIPLIER,
    TRAP_BOT_EXHAUSTION_CANDLE_MULTIPLIER,
    TRAP_BOT_CONFIDENCE_THRESHOLD,
)

log = get_logger("trap_bot")


@dataclass
class TrapCandidate:
    """A detected trap pattern candidate before confirmation."""
    trap_type: TrapType
    bar_index: int
    price: float
    direction: str  # "bull" or "bear"
    swing_level: float
    atr: float
    volume_ratio: float
    raw_score: float = 0.0


class TrapBotPipeline:
    """
    Complete 5-gate trap detection pipeline.

    Each gate must pass sequentially. A trade signal from this pipeline
    combined with a Layer 2 or Layer 3 crime pump alert produces the
    highest-confidence combined signal.
    """

    def __init__(self):
        self.lookback = TRAP_BOT_LOOKBACK_BARS
        self.ema_fast = TRAP_BOT_EMA_FAST
        self.ema_slow = TRAP_BOT_EMA_SLOW
        self.atr_period = TRAP_BOT_ATR_PERIOD
        self.volume_spike = TRAP_BOT_VOLUME_SPIKE_MULTIPLIER
        self.exhaustion_mult = TRAP_BOT_EXHAUSTION_CANDLE_MULTIPLIER
        self.confidence_threshold = TRAP_BOT_CONFIDENCE_THRESHOLD

    async def run_pipeline(self, ohlcv_data: pd.DataFrame) -> TrapDetectionResult:
        """
        Run the full 5-gate pipeline on OHLCV data.

        ohlcv_data must have columns: open, high, low, close, volume
        Indexed by timestamp.
        """
        result = TrapDetectionResult()
        gate_trace = []

        if ohlcv_data is None or len(ohlcv_data) < self.lookback:
            result.gate_trace = "Insufficient data for analysis"
            return result

        # Compute indicators
        df = self._compute_indicators(ohlcv_data)

        # ── Gate 1: Regime Classifier ─────────────────────────────────────────
        regime = self._classify_regime(df)
        result.regime = regime
        result.gate_1_passed = regime != Regime.UNKNOWN
        gate_trace.append(f"G1 Regime: {regime.value} -> {'PASS' if result.gate_1_passed else 'FAIL'}")

        if not result.gate_1_passed:
            result.gate_trace = " | ".join(gate_trace)
            return result

        # ── Gate 2: Location Filter ───────────────────────────────────────────
        location_pass, location_info = self._filter_location(df, regime)
        result.gate_2_passed = location_pass
        gate_trace.append(f"G2 Location: {location_info} -> {'PASS' if location_pass else 'FAIL'}")

        if not result.gate_2_passed:
            result.gate_trace = " | ".join(gate_trace)
            return result

        # ── Gate 3: Trap Detector ─────────────────────────────────────────────
        trap = self._detect_traps(df, regime)
        result.gate_3_passed = trap is not None
        if trap:
            result.trap_fired = trap.trap_type
            gate_trace.append(f"G3 Trap: {trap.trap_type.value} -> PASS")
        else:
            gate_trace.append("G3 Trap: None detected -> FAIL")
            result.gate_trace = " | ".join(gate_trace)
            return result

        # ── Gate 4: Confirmation Engine ───────────────────────────────────────
        confirmed, conf_info = self._confirm_trap(df, trap)
        result.gate_4_passed = confirmed
        gate_trace.append(f"G4 Confirm: {conf_info} -> {'PASS' if confirmed else 'FAIL'}")

        if not result.gate_4_passed:
            result.gate_trace = " | ".join(gate_trace)
            return result

        # ── Gate 5: Edge Model ────────────────────────────────────────────────
        confidence = self._score_edge(df, trap, regime)
        result.gate_5_passed = confidence >= self.confidence_threshold
        result.confidence = round(confidence, 3)
        gate_trace.append(f"G5 Edge: {confidence:.2f} -> {'PASS' if result.gate_5_passed else 'FAIL'}")

        result.gate_trace = " | ".join(gate_trace)

        if result.gate_5_passed:
            log.info("trap_detected", trap=result.trap_fired.value,
                    regime=result.regime.value, confidence=result.confidence)

        return result

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all required technical indicators."""
        df = df.copy()

        # EMAs
        df["ema_fast"] = df["close"].ewm(span=self.ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.ema_slow, adjust=False).mean()

        # ATR
        df["tr"] = np.maximum(
            df["high"] - df["low"],
            np.maximum(
                abs(df["high"] - df["close"].shift(1)),
                abs(df["low"] - df["close"].shift(1))
            )
        )
        df["atr"] = df["tr"].rolling(window=self.atr_period).mean()

        # Volume average
        df["vol_avg"] = df["volume"].rolling(window=20).mean()
        df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, 1)

        # Candle body and range
        df["body"] = abs(df["close"] - df["open"])
        df["range"] = df["high"] - df["low"]
        df["body_pct"] = df["body"] / df["range"].replace(0, 1)

        # Swing highs and lows (simplified)
        df["swing_high"] = df["high"].rolling(window=5, center=True).max()
        df["swing_low"] = df["low"].rolling(window=5, center=True).min()

        # Previous swing levels
        df["prev_high"] = df["high"].rolling(window=self.lookback).max()
        df["prev_low"] = df["low"].rolling(window=self.lookback).min()

        # EMA slope for trend direction
        df["ema_fast_slope"] = df["ema_fast"].diff(3) / df["atr"].replace(0, 1)

        return df

    def _classify_regime(self, df: pd.DataFrame) -> Regime:
        """
        Gate 1: Classify the current market regime.

        Trend: EMA fast consistently above/below EMA slow with positive slope
        Range: EMAs intertwined, price oscillating
        Unknown: Insufficient clarity
        """
        if len(df) < 20:
            return Regime.UNKNOWN

        recent = df.tail(20)

        # Check EMA alignment
        fast_above = (recent["ema_fast"] > recent["ema_slow"]).sum()
        fast_below = (recent["ema_fast"] < recent["ema_slow"]).sum()

        # Trend strength via slope
        avg_slope = recent["ema_fast_slope"].mean()

        if fast_above >= 16 and avg_slope > 0.3:
            return Regime.TREND  # Bullish trend
        elif fast_below >= 16 and avg_slope < -0.3:
            return Regime.TREND  # Bearish trend
        elif abs(fast_above - fast_below) <= 6:
            return Regime.RANGE
        else:
            return Regime.UNKNOWN

    def _filter_location(self, df: pd.DataFrame,
                          regime: Regime) -> tuple[bool, str]:
        """
        Gate 2: Filter based on price location relative to key levels.

        In a trend: price should be near the EMA zone
        In a range: price should be near range boundaries
        """
        if len(df) < 5:
            return False, "Insufficient data"

        current = df.iloc[-1]
        price = current["close"]
        atr = current["atr"]

        if atr <= 0:
            return False, "ATR is zero"

        if regime == Regime.TREND:
            # Distance from EMA zone in ATR units
            ema_mid = (current["ema_fast"] + current["ema_slow"]) / 2
            distance = abs(price - ema_mid) / atr

            if distance <= 2.0:
                return True, f"Price within 2 ATR of EMA zone ({distance:.1f} ATR)"
            else:
                return False, f"Price too far from EMA zone ({distance:.1f} ATR)"

        elif regime == Regime.RANGE:
            # Near range boundary
            range_size = current["prev_high"] - current["prev_low"]
            if range_size <= 0:
                return False, "Range size is zero"

            position = (price - current["prev_low"]) / range_size

            if position > 0.8 or position < 0.2:
                return True, f"Price near range boundary ({position:.0%} of range)"
            else:
                return False, f"Price in mid-range ({position:.0%} of range)"

        return False, "Unknown regime"

    def _detect_traps(self, df: pd.DataFrame,
                       regime: Regime) -> Optional[TrapCandidate]:
        """
        Gate 3: Detect trap patterns T1 through T5.

        Returns the strongest trap candidate found, or None.
        """
        if len(df) < 10:
            return None

        candidates = []

        # Check each trap type
        t1 = self._check_t1_failed_breakout(df)
        if t1:
            candidates.append(t1)

        t2 = self._check_t2_stop_sweep(df)
        if t2:
            candidates.append(t2)

        t3 = self._check_t3_giant_exhaustion(df)
        if t3:
            candidates.append(t3)

        t4 = self._check_t4_outside_bar(df)
        if t4:
            candidates.append(t4)

        if regime == Regime.TREND:
            t5 = self._check_t5_first_deep_pullback(df)
            if t5:
                candidates.append(t5)

        if not candidates:
            return None

        # Return highest scored candidate
        return max(candidates, key=lambda c: c.raw_score)

    def _check_t1_failed_breakout(self, df: pd.DataFrame) -> Optional[TrapCandidate]:
        """
        T1 Failed Breakout: Price breaks above/below a recent range boundary,
        fails to hold, and confirms reversal.

        Maps to crime pump pattern of a false move used to attract early traders.
        """
        current = df.iloc[-1]
        prev = df.iloc[-2]
        atr = current["atr"]
        prev_high = df["high"].iloc[-self.lookback:-1].max()
        prev_low = df["low"].iloc[-self.lookback:-1].min()

        # Bullish failed breakout (price broke below support then closed back above)
        if (prev["low"] < prev_low and
                current["close"] > prev_low and
                current["close"] > current["open"]):
            return TrapCandidate(
                trap_type=TrapType.T1_FAILED_BREAKOUT,
                bar_index=len(df) - 1,
                price=current["close"],
                direction="bull",
                swing_level=prev_low,
                atr=atr,
                volume_ratio=current["vol_ratio"],
                raw_score=0.7,
            )

        # Bearish failed breakout (price broke above resistance then closed back below)
        if (prev["high"] > prev_high and
                current["close"] < prev_high and
                current["close"] < current["open"]):
            return TrapCandidate(
                trap_type=TrapType.T1_FAILED_BREAKOUT,
                bar_index=len(df) - 1,
                price=current["close"],
                direction="bear",
                swing_level=prev_high,
                atr=atr,
                volume_ratio=current["vol_ratio"],
                raw_score=0.7,
            )

        return None

    def _check_t2_stop_sweep(self, df: pd.DataFrame) -> Optional[TrapCandidate]:
        """
        T2 Stop Sweep: Price briefly exceeds a recent swing high/low then closes
        back inside. This is the MOST CRITICAL trap type for crime pump detection
        as it directly mirrors the stop-hunt behavior seen in Stage Three.
        """
        current = df.iloc[-1]
        atr = current["atr"]

        # Find recent swing levels
        swing_high = df["high"].iloc[-20:-1].max()
        swing_low = df["low"].iloc[-20:-1].min()

        # Bullish stop sweep (swept below swing low, closed back inside)
        if (current["low"] < swing_low and
                current["close"] > swing_low and
                current["close"] > current["open"]):
            sweep_depth = (swing_low - current["low"]) / atr if atr > 0 else 0
            if 0 < sweep_depth < 1.5:  # Sweep should be shallow
                return TrapCandidate(
                    trap_type=TrapType.T2_STOP_SWEEP,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bull",
                    swing_level=swing_low,
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.85,  # Highest base score (most critical trap)
                )

        # Bearish stop sweep (swept above swing high, closed back inside)
        if (current["high"] > swing_high and
                current["close"] < swing_high and
                current["close"] < current["open"]):
            sweep_depth = (current["high"] - swing_high) / atr if atr > 0 else 0
            if 0 < sweep_depth < 1.5:
                return TrapCandidate(
                    trap_type=TrapType.T2_STOP_SWEEP,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bear",
                    swing_level=swing_high,
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.85,
                )

        return None

    def _check_t3_giant_exhaustion(self, df: pd.DataFrame) -> Optional[TrapCandidate]:
        """
        T3 Giant Exhaustion: An oversized candle at the end of a directional run
        with weak follow-through. Maps to Stage Four distribution signal where
        market makers sell into the final surge.
        """
        current = df.iloc[-1]
        prev = df.iloc[-2]
        atr = current["atr"]
        avg_body = df["body"].iloc[-20:].mean()

        if atr <= 0 or avg_body <= 0:
            return None

        body_ratio = current["body"] / (avg_body * self.exhaustion_mult)

        if body_ratio >= 1.0:
            # Check for weak close (close near the open of the candle range)
            candle_range = current["high"] - current["low"]
            if candle_range <= 0:
                return None

            if current["close"] > current["open"]:
                # Bullish giant candle, check for bearish exhaustion
                close_position = (current["close"] - current["low"]) / candle_range
                if close_position < 0.6:  # Close in lower 60% of range = weak
                    return TrapCandidate(
                        trap_type=TrapType.T3_GIANT_EXHAUSTION,
                        bar_index=len(df) - 1,
                        price=current["close"],
                        direction="bear",
                        swing_level=current["high"],
                        atr=atr,
                        volume_ratio=current["vol_ratio"],
                        raw_score=0.75,
                    )
            else:
                # Bearish giant candle, check for bullish exhaustion
                close_position = (current["close"] - current["low"]) / candle_range
                if close_position > 0.4:  # Close in upper 40% = weak
                    return TrapCandidate(
                        trap_type=TrapType.T3_GIANT_EXHAUSTION,
                        bar_index=len(df) - 1,
                        price=current["close"],
                        direction="bull",
                        swing_level=current["low"],
                        atr=atr,
                        volume_ratio=current["vol_ratio"],
                        raw_score=0.75,
                    )

        return None

    def _check_t4_outside_bar(self, df: pd.DataFrame) -> Optional[TrapCandidate]:
        """
        T4 Outside Bar Double Trap: An outside bar engulfs the prior bar trapping
        traders in both directions. Confirmation signal before Stage Three reversal.
        """
        current = df.iloc[-1]
        prev = df.iloc[-2]
        atr = current["atr"]

        # Outside bar: current bar's range engulfs previous bar
        if (current["high"] > prev["high"] and current["low"] < prev["low"]):
            # Determine direction by close
            if current["close"] > current["open"]:
                return TrapCandidate(
                    trap_type=TrapType.T4_OUTSIDE_BAR,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bull",
                    swing_level=current["low"],
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.65,
                )
            else:
                return TrapCandidate(
                    trap_type=TrapType.T4_OUTSIDE_BAR,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bear",
                    swing_level=current["high"],
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.65,
                )

        return None

    def _check_t5_first_deep_pullback(self, df: pd.DataFrame) -> Optional[TrapCandidate]:
        """
        T5 First Deep Pullback: In a trending regime, the first meaningful pullback
        to the EMA zone with rejection. Used to identify accumulation zones
        in Stage One and Two.
        """
        current = df.iloc[-1]
        prev = df.iloc[-2]
        atr = current["atr"]
        ema_fast = current["ema_fast"]
        ema_slow = current["ema_slow"]

        if atr <= 0:
            return None

        # Bullish trend: price pulled back to EMA zone and bounced
        if (ema_fast > ema_slow and
                prev["low"] <= ema_fast * 1.005 and  # Touched near fast EMA
                current["close"] > ema_fast and
                current["close"] > current["open"]):

            distance_from_ema = abs(prev["low"] - ema_fast) / atr
            if distance_from_ema < 0.5:
                return TrapCandidate(
                    trap_type=TrapType.T5_FIRST_DEEP_PULLBACK,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bull",
                    swing_level=prev["low"],
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.6,
                )

        # Bearish trend: price pulled back to EMA zone and rejected
        if (ema_fast < ema_slow and
                prev["high"] >= ema_fast * 0.995 and
                current["close"] < ema_fast and
                current["close"] < current["open"]):

            distance_from_ema = abs(prev["high"] - ema_fast) / atr
            if distance_from_ema < 0.5:
                return TrapCandidate(
                    trap_type=TrapType.T5_FIRST_DEEP_PULLBACK,
                    bar_index=len(df) - 1,
                    price=current["close"],
                    direction="bear",
                    swing_level=prev["high"],
                    atr=atr,
                    volume_ratio=current["vol_ratio"],
                    raw_score=0.6,
                )

        return None

    def _confirm_trap(self, df: pd.DataFrame,
                       trap: TrapCandidate) -> tuple[bool, str]:
        """
        Gate 4: Confirm the trap detection with volume, structure,
        and momentum checks.
        """
        confirmations = []

        # Volume confirmation
        if trap.volume_ratio >= self.volume_spike:
            confirmations.append(f"Volume spike ({trap.volume_ratio:.1f}x avg)")

        # Body-to-range ratio (strong close = more confirmation)
        current = df.iloc[-1]
        if current["range"] > 0:
            body_pct = current["body"] / current["range"]
            if body_pct > 0.6:
                confirmations.append(f"Strong close ({body_pct:.0%} body)")

        # Momentum alignment
        if trap.direction == "bull" and current["close"] > current["ema_fast"]:
            confirmations.append("Momentum aligned (above fast EMA)")
        elif trap.direction == "bear" and current["close"] < current["ema_fast"]:
            confirmations.append("Momentum aligned (below fast EMA)")

        confirmed = len(confirmations) >= 2
        info = ", ".join(confirmations) if confirmations else "No confirmations"

        return confirmed, info

    def _score_edge(self, df: pd.DataFrame, trap: TrapCandidate,
                     regime: Regime) -> float:
        """
        Gate 5: Final confidence scoring combining all factors.
        """
        score = trap.raw_score

        # Volume boost
        if trap.volume_ratio >= self.volume_spike * 1.5:
            score += 0.1
        elif trap.volume_ratio >= self.volume_spike:
            score += 0.05

        # Regime alignment boost
        if regime == Regime.TREND:
            score += 0.05

        # T2 (stop sweep) gets extra weight due to crime pump significance
        if trap.trap_type == TrapType.T2_STOP_SWEEP:
            score += 0.1

        # Normalize to 0-1
        return min(max(score, 0.0), 1.0)
