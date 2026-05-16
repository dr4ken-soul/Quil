"""
Crime Coin Lifecycle Tracker

Tracks which of the four stages a crime coin is currently in based on
the signals from all four detection layers. This is the core detection
framework that ties all signals together into an actionable classification.

The Four Stages:
1. Accumulation and Initial Pump - OI rises, price rises, goal is attracting shorts
2. OI and Price Matrix - Cross-referencing all four OI-price combinations
3. The Trap (Most Critical) - Fake exhaustion to lure shorts before reversal
4. Distribution - Selling, shorting, and extracting final profits
"""

from models.token import Token
from models.alert import DetectionLayerResult
from models import CrimeStage
from utils.logger import get_logger

log = get_logger("lifecycle")


class LifecycleTracker:
    """
    Determines which stage of the crime coin lifecycle a token is in
    based on aggregated signals from all detection layers.
    """

    def __init__(self):
        # Store historical state for tokens being tracked
        self._token_history: dict[str, list[dict]] = {}

    def classify_stage(self, token: Token,
                        layer_results: list[DetectionLayerResult]) -> dict:
        """
        Classify the current crime coin stage based on all available data.

        Returns a dict with:
        - stage: CrimeStage enum value
        - confidence: float 0-1
        - reasoning: str explanation
        - alert_priority: str (watch/elevated/critical)
        """
        # Collect all signal data
        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        funding = token.derivatives.funding_rate
        ls_ratio = token.derivatives.long_short_ratio
        vol_1h = token.metrics.volume_1h
        vol_24h = token.metrics.volume_24h

        # Get layer scores
        layer_scores = {r.layer_number: r.score for r in layer_results}
        layer_triggered = {r.layer_number: r.triggered for r in layer_results}

        # Score each stage
        stage_scores = {
            CrimeStage.STAGE_ONE: self._score_stage_one(token, layer_scores),
            CrimeStage.STAGE_TWO: self._score_stage_two(token, layer_scores),
            CrimeStage.STAGE_THREE: self._score_stage_three(token, layer_scores),
            CrimeStage.STAGE_FOUR: self._score_stage_four(token, layer_scores),
        }

        # Determine the most likely stage
        best_stage = max(stage_scores, key=stage_scores.get)
        best_score = stage_scores[best_stage]

        if best_score < 0.3:
            best_stage = CrimeStage.NONE

        # Determine alert priority
        if best_score >= 0.7:
            priority = "critical"
        elif best_score >= 0.5:
            priority = "elevated"
        elif best_score >= 0.3:
            priority = "watch"
        else:
            priority = "none"

        reasoning = self._build_stage_reasoning(
            best_stage, best_score, stage_scores, token, layer_results
        )

        # Update token history
        self._update_history(token.symbol, best_stage, best_score)

        return {
            "stage": best_stage,
            "confidence": round(best_score, 3),
            "reasoning": reasoning,
            "alert_priority": priority,
            "all_stage_scores": {s.value: round(v, 3) for s, v in stage_scores.items()},
        }

    def _score_stage_one(self, token: Token, layer_scores: dict) -> float:
        """
        Stage One: Accumulation and Initial Pump

        OI rises and price rises. Longs push the price up with little resistance.
        The market maker's goal at this stage is to attract short sellers.
        If there are not enough shorts they may sell directly to liquidate longs.
        """
        score = 0.0

        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        price_change_24h = token.metrics.price_change_24h
        supply_concentration = token.onchain.top10_wallet_pct

        # Rising OI + rising price = long opening (Stage 1 pattern)
        if oi_change > 10 and price_change > 5:
            score += 0.3
        elif oi_change > 5 and price_change > 2:
            score += 0.15

        # MULTI-DAY MOMENTUM: 24h price change catches gradual pumps
        # that individual 1h snapshots miss
        if price_change_24h > 50:
            score += 0.35  # Massive 24h move — strong crime pump signal
        elif price_change_24h > 30:
            score += 0.25
        elif price_change_24h > 15:
            score += 0.15

        # Volume/MarketCap ratio — abnormally high volume vs market cap
        # is a key sign of coordinated pump activity
        mcap = token.metrics.market_cap
        vol_24h = token.metrics.volume_24h
        if mcap > 0 and vol_24h > 0:
            vol_mcap_ratio = vol_24h / mcap
            if vol_mcap_ratio > 1.0:
                score += 0.25  # Volume exceeds market cap — extreme
            elif vol_mcap_ratio > 0.5:
                score += 0.15
            elif vol_mcap_ratio > 0.2:
                score += 0.1

        # High supply concentration (accumulation complete)
        if supply_concentration > 80:
            score += 0.25
        elif supply_concentration > 60:
            score += 0.1

        # Layer 1 (on-chain) should be triggered
        if layer_scores.get(1, 0) > 0.4:
            score += 0.25

        # Young token with low liquidity
        if (token.onchain.contract_age_days is not None and
                token.onchain.contract_age_days < 30):
            score += 0.1

        # Low funding (not yet negative, shorts havent entered en masse)
        funding = token.derivatives.funding_rate
        if -0.005 < funding < 0.005:
            score += 0.1

        return min(score, 1.0)

    def _score_stage_two(self, token: Token, layer_scores: dict) -> float:
        """
        Stage Two: OI and Price Matrix

        Cross-referencing all four OI-price combinations.
        Multiple scenarios can drive OI changes including hedging.
        No single variable is sufficient on its own.
        """
        score = 0.0

        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        ls_change = token.derivatives.long_short_ratio_change_1h

        # OI rising significantly (new positions being opened)
        if abs(oi_change) > 15:
            score += 0.2

        # Layer 2 (perp) signals present
        if layer_scores.get(2, 0) > 0.3:
            score += 0.25

        # Moderate negative funding emerging (shorts beginning to enter)
        funding = token.derivatives.funding_rate
        if -0.015 < funding < -0.005:
            score += 0.2

        # OI change without corresponding L/S change (hedging)
        if abs(oi_change) > 10 and abs(ls_change) < 3:
            score += 0.2

        # Layer 1 still active
        if layer_scores.get(1, 0) > 0.3:
            score += 0.15

        return min(score, 1.0)

    def _score_stage_three(self, token: Token, layer_scores: dict) -> float:
        """
        Stage Three: The Trap (Most Critical Detection Point)

        The key pattern seen in $RAVE: market makers close shorts or unwind hedges
        to create a drop in OI alongside a price drop with increased volume.
        This makes the move look exhausted and complete. Short sellers enter.
        Price then rapidly reverses and continues higher trapping all newly opened shorts.

        This is the most critical detection point. The hl-trap-bot T2 Stop Sweep
        and T3 Giant Exhaustion trap detectors are directly applicable here.
        """
        score = 0.0

        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        vol_1h = token.metrics.volume_1h
        vol_24h = token.metrics.volume_24h
        funding = token.derivatives.funding_rate

        # THE TRAP PATTERN: OI drops + volume spikes + temporary price dip
        vol_spike = vol_1h > (vol_24h / 24 * 3) if vol_24h > 0 else False

        if oi_change < -10 and vol_spike:
            # OI dropping with volume surge = possible trap being set
            score += 0.35

        # Deeply negative funding (shorts are committed)
        if funding <= -0.015:
            score += 0.25
        elif funding <= -0.01:
            score += 0.15

        # Layer 3 (funding) triggered
        if layer_scores.get(3, 0) > 0.4:
            score += 0.2

        # Layer 4 (order book) showing liquidation cascade potential
        if layer_scores.get(4, 0) > 0.3:
            score += 0.15

        # Historical pattern: recent price decline followed by stabilization
        # (The "looks exhausted" phase before reversal)
        if -15 < token.metrics.price_change_24h < -5 and abs(price_change) < 3:
            score += 0.1

        return min(score, 1.0)

    def _score_stage_four(self, token: Token, layer_scores: dict) -> float:
        """
        Stage Four: Distribution

        Market makers establish short positions, close longs, and sell spot holdings.
        Indicators: funding approaching -2%, sharp OI drop, on-chain spot deposits
        into exchanges.
        """
        score = 0.0

        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        funding = token.derivatives.funding_rate

        # Funding at or beyond critical level
        if funding <= -0.02:
            score += 0.3
        elif funding <= -0.015:
            score += 0.15

        # Sharp OI drop (positions being closed)
        if oi_change < -20:
            score += 0.25
        elif oi_change < -10:
            score += 0.15

        # Price declining after a significant run
        if price_change < -5 and token.metrics.price_change_24h > 20:
            score += 0.2

        # Layer 4 active (liquidation cascades, distribution)
        if layer_scores.get(4, 0) > 0.4:
            score += 0.15

        # Cold wallet deposits to exchanges (on-chain sell signal)
        if token.onchain.cold_wallet_outflows_24h < 0:  # Negative = inflows to exchange
            score += 0.1

        return min(score, 1.0)

    def _build_stage_reasoning(self, stage: CrimeStage, confidence: float,
                                all_scores: dict, token: Token,
                                layer_results: list[DetectionLayerResult]) -> str:
        """Build a detailed reasoning explanation for the stage classification."""
        if stage == CrimeStage.NONE:
            return ("No crime coin lifecycle stage detected. The token does not match "
                   "any of the four stage patterns with sufficient confidence.")

        triggered_layers = [r for r in layer_results if r.triggered]
        layer_names = [r.layer_name for r in triggered_layers]

        parts = []

        if stage == CrimeStage.STAGE_ONE:
            parts.append(f"Token classified as Stage One (Accumulation and Initial Pump) "
                        f"with {confidence:.0%} confidence.")
            parts.append("OI and price are rising together indicating new long positions "
                        "are pushing price up with minimal resistance.")
            parts.append("The market maker's goal at this stage is to attract short sellers. "
                        "If insufficient shorts enter they may sell directly to liquidate longs.")

        elif stage == CrimeStage.STAGE_TWO:
            parts.append(f"Token classified as Stage Two (OI and Price Matrix) "
                        f"with {confidence:.0%} confidence.")
            parts.append("Multiple OI-price scenarios are being observed. Market maker "
                        "hedging may be inflating OI figures. No single variable is being "
                        "used in isolation for this assessment.")

        elif stage == CrimeStage.STAGE_THREE:
            parts.append(f"Token classified as STAGE THREE (THE TRAP) "
                        f"with {confidence:.0%} confidence. This is the most critical "
                        f"detection point.")
            parts.append("The pattern matches the $RAVE model: market makers may be "
                        "closing shorts or unwinding hedges to create a drop in OI "
                        "alongside a price dip with increased volume. This is designed "
                        "to make the move look exhausted so short sellers enter. "
                        "Price is expected to reverse and continue higher, trapping "
                        "all newly opened shorts.")

        elif stage == CrimeStage.STAGE_FOUR:
            parts.append(f"Token classified as Stage Four (Distribution) "
                        f"with {confidence:.0%} confidence.")
            parts.append("Market makers are establishing short positions, closing longs, "
                        "and selling spot holdings. Funding rates are approaching or "
                        "have reached the critical -2% level.")

        if triggered_layers:
            parts.append(f"Detection layers triggered: {', '.join(layer_names)}.")

        return " ".join(parts)

    def _update_history(self, symbol: str, stage: CrimeStage, score: float) -> None:
        """Track stage transitions for a token."""
        if symbol not in self._token_history:
            self._token_history[symbol] = []

        from datetime import datetime
        self._token_history[symbol].append({
            "timestamp": datetime.utcnow().isoformat(),
            "stage": stage.value,
            "score": score,
        })

        # Keep only last 100 entries per token
        if len(self._token_history[symbol]) > 100:
            self._token_history[symbol] = self._token_history[symbol][-100:]

    def get_stage_transitions(self, symbol: str) -> list[dict]:
        """Get the stage transition history for a token."""
        return self._token_history.get(symbol, [])
