"""
Detection Layer 3: Funding Rate Dynamics

Analyzes funding rate patterns across exchanges to detect crime pump setups.

Market makers with effectively unlimited margin profit by pushing spot prices
on exchanges with the highest index weight to create deeply negative funding rates.
This forces short holders to pay funding continuously.

A negative 2% funding rate every 4 hours translates to approximately 12% per day
paid by shorts. The bot flags funding rates reaching -1.5% or deeper as a strong
crime pump signal especially when combined with concentrated spot activity on
Bitget, Gate, or Aster.
"""

from typing import Optional
from models.token import Token
from models.alert import DetectionLayerResult
from utils.logger import get_logger
from config.settings import (
    FUNDING_RATE_ALERT_THRESHOLD,
    FUNDING_RATE_CRITICAL_THRESHOLD,
    FUNDING_CHECK_INTERVAL_HOURS,
)

log = get_logger("layer3_funding")


class FundingRateAnalyzer:
    """
    Detects funding rate manipulation patterns used in crime pump operations.

    Negative funding can originate from three sources:
    1. Retail short traders
    2. Market makers shorting while distributing
    3. Market makers pushing spot higher deliberately to force shorts to pay

    The bot assesses which source is most likely based on the on-chain and OI
    context before concluding.
    """

    def __init__(self):
        self.layer_number = 3
        self.layer_name = "Funding Rate Dynamics"

    async def analyze(self, token: Token,
                       funding_data: list = None) -> DetectionLayerResult:
        """
        Run all Layer 3 funding rate checks.
        """
        result = DetectionLayerResult(
            layer_number=self.layer_number,
            layer_name=self.layer_name,
        )

        signals = []
        total_score = 0.0
        max_score = 5.0

        # ── Signal 1: Current Funding Rate Level ──────────────────────────────
        score, signal = self._check_current_funding(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 2: Funding Rate Trend ──────────────────────────────────────
        score, signal = self._check_funding_trend(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 3: Funding Source Analysis ─────────────────────────────────
        score, signal = self._analyze_funding_source(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 4: Cross-Exchange Funding Divergence ───────────────────────
        score, signal = self._check_cross_exchange_funding(token, funding_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 5: Funding Rate + Spot Activity Combination ────────────────
        score, signal = self._check_funding_spot_combo(token)
        if signal:
            signals.append(signal)
            total_score += score

        normalized_score = total_score / max_score if max_score > 0 else 0

        result.signals = signals
        result.score = round(normalized_score, 3)
        result.triggered = normalized_score >= 0.4
        result.reasoning = self._build_reasoning(signals, normalized_score, token)

        if result.triggered:
            log.info("layer3_triggered", token=token.symbol, score=result.score,
                    signals_count=len(signals))

        return result

    def _check_current_funding(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check current funding rate against alert thresholds.
        -1.5% is alert level, -2% is critical.
        """
        funding = token.derivatives.funding_rate

        if funding <= FUNDING_RATE_CRITICAL_THRESHOLD:
            daily_cost = abs(funding) * (24 / FUNDING_CHECK_INTERVAL_HOURS) * 100
            return 1.0, {
                "name": "Critical Negative Funding Rate",
                "observation": f"Funding rate at {funding * 100:.2f}% per "
                             f"{FUNDING_CHECK_INTERVAL_HOURS}h period. "
                             f"Shorts paying approximately {daily_cost:.1f}% per day.",
                "significance": "Funding rate has reached the critical -2% threshold. "
                              "At this level shorts are paying massive funding costs "
                              "continuously. This is the exact mechanism market makers "
                              "use to extract capital from short holders during a crime pump. "
                              "Combined with concentrated spot activity this is one of "
                              "the strongest crime pump signals available.",
            }
        elif funding <= FUNDING_RATE_ALERT_THRESHOLD:
            daily_cost = abs(funding) * (24 / FUNDING_CHECK_INTERVAL_HOURS) * 100
            return 0.7, {
                "name": "Deeply Negative Funding Rate",
                "observation": f"Funding rate at {funding * 100:.2f}% per "
                             f"{FUNDING_CHECK_INTERVAL_HOURS}h. "
                             f"Shorts paying approximately {daily_cost:.1f}% per day.",
                "significance": "Funding rate exceeds the -1.5% alert threshold. "
                              "Shorts are under significant pressure from funding costs. "
                              "This level often escalates to the critical -2% zone "
                              "before the forced move begins.",
            }
        elif funding < -0.005:
            return 0.3, {
                "name": "Elevated Negative Funding",
                "observation": f"Funding rate at {funding * 100:.2f}%",
                "significance": "Moderately negative funding. Not yet at alert levels but "
                              "trending in the direction that precedes crime pump setups.",
            }
        return 0, None

    def _check_funding_trend(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check if funding is trending more negative over time.
        A consistently deepening negative funding trend is a preparation signal.
        """
        current = token.derivatives.funding_rate
        avg_7d = token.derivatives.funding_rate_avg_7d

        if avg_7d == 0 or current == 0:
            return 0, None

        # Current funding significantly more negative than 7d average
        if current < avg_7d and current < -0.005:
            deviation = abs(current - avg_7d)
            if deviation > 0.01:
                return 1.0, {
                    "name": "Funding Rate Deterioration",
                    "observation": f"Current funding ({current * 100:.2f}%) is significantly "
                                 f"more negative than 7d average ({avg_7d * 100:.2f}%)",
                    "significance": "The funding rate is actively deteriorating beyond its "
                                  "recent average. This trend indicates increasing short-side "
                                  "pressure and a growing imbalance that market makers "
                                  "can exploit for liquidation cascades.",
                }
            elif deviation > 0.005:
                return 0.5, {
                    "name": "Funding Rate Trending Negative",
                    "observation": f"Funding trending more negative than 7d average",
                    "significance": "Gradual funding deterioration detected. Monitoring for "
                                  "acceleration toward critical levels.",
                }
        return 0, None

    def _analyze_funding_source(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Determine the most likely source of negative funding:
        1. Retail shorts (organic bearish sentiment)
        2. Market makers shorting while distributing (Stage 4)
        3. Market makers pushing spot to force shorts to pay (most dangerous)

        The assessment uses OI and price context.
        """
        funding = token.derivatives.funding_rate
        if funding >= -0.005:
            return 0, None

        oi_change = token.derivatives.oi_change_1h
        price_change = token.metrics.price_change_1h
        ls_ratio = token.derivatives.long_short_ratio

        # Scenario 3: Spot pushing + negative funding + rising price = crime pump
        if price_change > 5 and funding <= FUNDING_RATE_ALERT_THRESHOLD:
            return 1.0, {
                "name": "Market Maker Spot Push Detected",
                "observation": f"Price up {price_change:.1f}% while funding at "
                             f"{funding * 100:.2f}%. Market makers likely pushing spot "
                             f"to create negative funding and extract from shorts.",
                "significance": "The most dangerous funding source. Market makers are "
                              "deliberately pushing spot prices higher to create deeply "
                              "negative funding. Shorts are being forced to pay while "
                              "price moves against them. This is the primary profit "
                              "extraction mechanism in crime pump operations.",
            }

        # Scenario 2: Falling OI + negative funding + falling price = distribution
        if oi_change < -5 and price_change < 0:
            return 0.5, {
                "name": "Distribution Phase Funding",
                "observation": f"Negative funding ({funding * 100:.2f}%) with falling OI "
                             f"({oi_change:+.1f}%) and declining price",
                "significance": "This funding pattern may indicate Stage 4 distribution "
                              "where market makers are shorting while selling spot holdings.",
            }

        # Scenario 1: Default retail short pressure
        return 0.3, {
            "name": "Retail Short Pressure",
            "observation": f"Negative funding ({funding * 100:.2f}%) likely from retail "
                         f"short positioning (L/S ratio: {ls_ratio:.2f})",
            "significance": "Negative funding appears to be from organic short sentiment. "
                          "However this creates the exact conditions that attract crime "
                          "pump operations as shorts become the funding source for "
                          "market maker profits.",
        }

    def _check_cross_exchange_funding(self, token: Token,
                                       funding_data: list = None) -> tuple[float, Optional[dict]]:
        """
        Check for significant funding rate differences between exchanges.
        Divergence can indicate targeted manipulation on specific venues.
        """
        if not funding_data or not isinstance(funding_data, list):
            return 0, None

        rates = {}
        for entry in funding_data:
            exchange = entry.get("exchange", entry.get("exchangeName", ""))
            rate = entry.get("fundingRate", entry.get("rate", 0))
            if exchange and rate is not None:
                try:
                    rates[exchange.lower()] = float(rate)
                except (ValueError, TypeError):
                    continue

        if len(rates) < 2:
            return 0, None

        max_rate = max(rates.values())
        min_rate = min(rates.values())
        spread = max_rate - min_rate

        if spread > 0.015:  # >1.5% spread between exchanges
            most_negative_ex = min(rates, key=rates.get)
            most_positive_ex = max(rates, key=rates.get)
            return 1.0, {
                "name": "Cross-Exchange Funding Divergence",
                "observation": f"Funding spread of {spread * 100:.2f}% between "
                             f"{most_positive_ex.title()} ({rates[most_positive_ex] * 100:+.2f}%) "
                             f"and {most_negative_ex.title()} ({rates[most_negative_ex] * 100:+.2f}%)",
                "significance": "Significant funding rate divergence between exchanges "
                              "indicates targeted manipulation on specific venues. "
                              "Market makers may be pushing spot on one exchange "
                              "to influence funding rates on another.",
            }
        return 0, None

    def _check_funding_spot_combo(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check the combination of negative funding with concentrated spot activity
        on weak-control exchanges (Bitget, Gate, Aster).
        """
        funding = token.derivatives.funding_rate
        if funding >= FUNDING_RATE_ALERT_THRESHOLD:
            return 0, None

        primary_exchange = token.exchange_presence.primary_spot_exchange
        vol_dist = token.exchange_presence.exchange_volume_distribution

        from config.exchanges import CRIME_PUMP_SPOT_EXCHANGES

        # Check if primary volume is on a weak-control exchange
        if primary_exchange and primary_exchange.lower() in CRIME_PUMP_SPOT_EXCHANGES:
            return 1.0, {
                "name": "Funding + Weak Exchange Spot Activity",
                "observation": f"Negative funding ({funding * 100:.2f}%) combined with "
                             f"primary spot activity on {primary_exchange.title()}",
                "significance": "Deeply negative funding paired with concentrated spot "
                              "trading on an exchange with weak risk controls is one of "
                              "the highest confidence crime pump signal combinations. "
                              "This is the exact setup used in confirmed cases like "
                              "MYX, COAI, and SIREN.",
            }

        # Check if any weak exchange has significant volume share
        if vol_dist:
            for ex in CRIME_PUMP_SPOT_EXCHANGES:
                if vol_dist.get(ex, 0) > 0.3:  # >30% volume share
                    return 0.7, {
                        "name": "Funding + Elevated Weak Exchange Volume",
                        "observation": f"Negative funding with {ex.title()} handling "
                                     f"{vol_dist[ex] * 100:.0f}% of spot volume",
                        "significance": "Significant spot volume flowing through a "
                                      "weak-control exchange during negative funding conditions.",
                    }
        return 0, None

    def _build_reasoning(self, signals: list[dict], score: float,
                          token: Token) -> str:
        """Build reasoning summary for Layer 3."""
        if not signals:
            return "Funding rates are within normal ranges across all exchanges. " \
                   "No manipulation signals detected in the funding dynamics."

        parts = []
        parts.append(f"Layer 3 analysis identified {len(signals)} signal(s) "
                    f"with a combined score of {score:.2f}.")

        for sig in signals:
            parts.append(f"{sig['name']}: {sig['observation']}")

        funding = token.derivatives.funding_rate
        if funding < 0:
            daily = abs(funding) * (24 / FUNDING_CHECK_INTERVAL_HOURS) * 100
            parts.append(f"At the current funding rate shorts are paying "
                        f"approximately {daily:.1f}% per day to hold their positions.")

        if score >= 0.7:
            parts.append("The funding rate dynamics show strong indicators of manipulation. "
                        "The combination of deeply negative rates with the observed market "
                        "context matches the profit extraction patterns seen in confirmed "
                        "crime pump cases.")
        elif score >= 0.4:
            parts.append("Funding conditions are deteriorating and worth monitoring closely "
                        "for further escalation toward critical levels.")

        return " ".join(parts)
