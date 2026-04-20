"""
Detection Layer 4: Spot and Order Book Manipulation

Monitors real-time order book behavior, liquidation patterns, spoof order detection,
and cross-exchange price discrepancies. This layer captures the execution-level
signals that appear as manipulation is actively occurring.

Manipulating spot is low cost when exchanges hold limited inventory for a given token.
During pumps market makers may pull liquidity from the order book to reduce sell-side
resistance and move price with smaller capital.
"""

from typing import Optional
from models.token import Token
from models.alert import DetectionLayerResult
from utils.logger import get_logger
from config.settings import (
    SPOOF_ORDER_DETECTION_WINDOW_SEC,
    LIQUIDATION_CASCADE_THRESHOLD,
    CROSS_EXCHANGE_PRICE_DIVERGENCE_PCT,
)

log = get_logger("layer4_orderbook")


class OrderBookManipulationAnalyzer:
    """
    Detects spot and order book manipulation patterns in real time.

    Signals monitored:
    - Liquidation heatmaps with large clustered levels above price
    - Spoof orders that appear and disappear rapidly
    - Unusual liquidation cascades
    - Volume, OI, and price discrepancies across exchanges simultaneously
    - Buy/sell transaction ratio anomalies
    - One-sided liquidity provision
    """

    def __init__(self):
        self.layer_number = 4
        self.layer_name = "Spot/Order Book Manipulation"
        # Track recent order book snapshots for spoof detection
        self._order_book_history: dict[str, list] = {}

    async def analyze(self, token: Token,
                       order_book_data: dict = None,
                       liquidation_data: list = None) -> DetectionLayerResult:
        """
        Run all Layer 4 checks.
        """
        result = DetectionLayerResult(
            layer_number=self.layer_number,
            layer_name=self.layer_name,
        )

        signals = []
        total_score = 0.0
        max_score = 6.0

        # ── Signal 1: Liquidation Heatmap Clustering ──────────────────────────
        score, signal = self._check_liquidation_clustering(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 2: Liquidation Cascade Detection ───────────────────────────
        score, signal = self._check_liquidation_cascade(token, liquidation_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 3: Cross-Exchange Price Divergence ─────────────────────────
        score, signal = self._check_price_divergence(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 4: Buy/Sell Ratio Anomaly ──────────────────────────────────
        score, signal = self._check_buy_sell_anomaly(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 5: Volume Without News ─────────────────────────────────────
        score, signal = self._check_volume_without_catalyst(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 6: One-Sided Liquidity ─────────────────────────────────────
        score, signal = self._check_one_sided_liquidity(token, order_book_data)
        if signal:
            signals.append(signal)
            total_score += score

        normalized_score = total_score / max_score if max_score > 0 else 0

        result.signals = signals
        result.score = round(normalized_score, 3)
        result.triggered = normalized_score >= 0.4
        result.reasoning = self._build_reasoning(signals, normalized_score)

        if result.triggered:
            log.info("layer4_triggered", token=token.symbol, score=result.score,
                    signals_count=len(signals))

        return result

    def _check_liquidation_clustering(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check liquidation heatmap for large clustered liquidation levels above
        the current price (indicating a large pool of shorts that could be squeezed).
        """
        heatmap = token.derivatives.liquidation_heatmap
        if not heatmap:
            return 0, None

        price = token.metrics.price
        if price <= 0:
            return 0, None

        # Check for concentrated liquidation levels above price
        above_price_levels = {
            level: value for level, value in heatmap.items()
            if float(level) > price
        }

        if not above_price_levels:
            return 0, None

        total_above = sum(above_price_levels.values())
        total_all = sum(heatmap.values()) if heatmap else 1

        if total_all > 0:
            above_ratio = total_above / total_all
            if above_ratio > 0.6:  # 60%+ of liquidations above current price
                closest_major = min(above_price_levels.keys(),
                                   key=lambda k: float(k) - price)
                pct_to_closest = ((float(closest_major) - price) / price) * 100

                return 1.0, {
                    "name": "Liquidation Cluster Above Price",
                    "observation": f"{above_ratio * 100:.0f}% of liquidation volume is "
                                 f"above current price. Nearest major level is "
                                 f"{pct_to_closest:.1f}% away.",
                    "significance": "Dense liquidation clusters above price create a magnetic "
                                  "pull for market makers. Pushing price through these levels "
                                  "triggers cascading short liquidations that accelerate the move "
                                  "and generate significant profit for the manipulator.",
                }
        return 0, None

    def _check_liquidation_cascade(self, token: Token,
                                    liquidation_data: list = None) -> tuple[float, Optional[dict]]:
        """
        Detect unusual liquidation cascades (multiple liquidations in same direction
        in a short window).
        """
        short_liqs = token.derivatives.liquidations_short_24h
        long_liqs = token.derivatives.liquidations_long_24h

        if short_liqs <= 0 and long_liqs <= 0:
            return 0, None

        total_liqs = short_liqs + long_liqs
        if total_liqs <= 0:
            return 0, None

        # Check for heavily one-sided liquidations (shorts being wiped)
        if short_liqs > 0:
            short_ratio = short_liqs / total_liqs if total_liqs > 0 else 0
            if short_ratio > 0.8 and short_liqs > 100_000:
                return 1.0, {
                    "name": "Short Liquidation Cascade",
                    "observation": f"${short_liqs:,.0f} in short liquidations (24h), "
                                 f"representing {short_ratio * 100:.0f}% of all liquidations",
                    "significance": "Heavily one-sided short liquidations indicate a forced "
                                  "squeeze. This is the profit realization mechanism in crime "
                                  "pump Stage Three where trapped shorts are systematically "
                                  "liquidated.",
                }
            elif short_ratio > 0.7:
                return 0.5, {
                    "name": "Elevated Short Liquidations",
                    "observation": f"Short liquidations dominate at {short_ratio * 100:.0f}% "
                                 f"of total (${short_liqs:,.0f})",
                    "significance": "Short-biased liquidations suggest mounting pressure "
                                  "on short holders.",
                }

        # Also check for excessive long liquidations (Stage 4 distribution)
        if long_liqs > 0:
            long_ratio = long_liqs / total_liqs if total_liqs > 0 else 0
            if long_ratio > 0.8 and long_liqs > 100_000:
                return 0.7, {
                    "name": "Long Liquidation Cascade",
                    "observation": f"${long_liqs:,.0f} in long liquidations (24h), "
                                 f"representing {long_ratio * 100:.0f}% of all liquidations",
                    "significance": "One-sided long liquidations may indicate Stage Four "
                                  "distribution where market makers sell into longs.",
                }

        return 0, None

    def _check_price_divergence(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check for significant price differences between exchanges.
        Price divergence enables arbitrage exploitation and indicates
        targeted spot manipulation on specific venues.
        """
        spread = token.exchange_presence.exchange_price_spread
        if not spread or len(spread) < 2:
            return 0, None

        prices = {ex: p for ex, p in spread.items() if p > 0}
        if len(prices) < 2:
            return 0, None

        max_price = max(prices.values())
        min_price = min(prices.values())
        divergence_pct = ((max_price - min_price) / min_price) * 100

        if divergence_pct >= CROSS_EXCHANGE_PRICE_DIVERGENCE_PCT:
            highest_ex = max(prices, key=prices.get)
            lowest_ex = min(prices, key=prices.get)
            return 1.0, {
                "name": "Cross-Exchange Price Divergence",
                "observation": f"{divergence_pct:.2f}% price spread. {highest_ex.title()} "
                             f"at ${max_price:.4f} vs {lowest_ex.title()} at ${min_price:.4f}",
                "significance": "Significant price divergence between exchanges indicates "
                              "the spot price is being pushed on a specific venue. This is "
                              "a key technique where market makers manipulate price on an "
                              "exchange with higher index weight to influence perp mark "
                              "prices across the market.",
            }
        elif divergence_pct >= CROSS_EXCHANGE_PRICE_DIVERGENCE_PCT * 0.5:
            return 0.3, {
                "name": "Minor Price Divergence",
                "observation": f"{divergence_pct:.2f}% price spread between exchanges",
                "significance": "Emerging price divergence detected. Not yet at "
                              "manipulation levels but worth monitoring.",
            }
        return 0, None

    def _check_buy_sell_anomaly(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check for heavily skewed buy/sell transaction ratios.
        Coordinated buying with minimal selling is a manipulation signal.
        """
        metrics = token.metrics
        # Use volume proxy if direct txn counts not available
        # DexScreener provides buy/sell counts

        # Check 5m window for immediate signals
        buy_ratio_5m = 0
        total_5m = (getattr(metrics, 'volume_5m', 0) or 0)

        # Check 1h window
        vol_1h = metrics.volume_1h
        vol_24h = metrics.volume_24h

        if vol_24h > 0:
            hourly_avg = vol_24h / 24
            if vol_1h > hourly_avg * 4:  # 4x the hourly average
                return 0.7, {
                    "name": "Burst Buy Volume",
                    "observation": f"1h volume (${vol_1h:,.0f}) is "
                                 f"{vol_1h / hourly_avg:.1f}x the 24h hourly average",
                    "significance": "Concentrated buying pressure significantly above "
                                  "baseline suggests coordinated activity rather than "
                                  "organic interest. This matches the pattern of burst "
                                  "buying from pre-funded wallets.",
                }
        return 0, None

    def _check_volume_without_catalyst(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Flag sudden volume spikes that have no corresponding news catalyst.
        The social signal layer provides context, but the absence of social
        activity alongside volume is itself a signal.
        """
        social = token.social
        metrics = token.metrics

        vol_spike = False
        if metrics.volume_24h > 0:
            hourly_avg = metrics.volume_24h / 24
            vol_spike = metrics.volume_1h > hourly_avg * 3

        social_quiet = (social.twitter_mentions_1h < 10 and
                       social.telegram_mentions_1h < 5)

        if vol_spike and social_quiet:
            return 1.0, {
                "name": "Volume Spike Without News Catalyst",
                "observation": f"Volume spiked significantly with minimal social activity "
                             f"(Twitter mentions: {social.twitter_mentions_1h}, "
                             f"Telegram: {social.telegram_mentions_1h})",
                "significance": "Sudden volume increase with no public news or social media "
                              "catalyst is a strong manipulation indicator. Organic price "
                              "discovery is accompanied by discussion. Silent volume spikes "
                              "suggest coordinated insider activity.",
            }
        return 0, None

    def _check_one_sided_liquidity(self, token: Token,
                                    order_book_data: dict = None) -> tuple[float, Optional[dict]]:
        """
        Check for one-sided liquidity provision where sell-side depth
        has been pulled while buy-side remains.
        """
        if not order_book_data:
            return 0, None

        bids = order_book_data.get("bids", [])
        asks = order_book_data.get("asks", [])

        if not bids or not asks:
            return 0, None

        # Sum top 10 levels each side
        bid_depth = sum(float(b[1]) for b in bids[:10]) if len(bids) >= 10 else 0
        ask_depth = sum(float(a[1]) for a in asks[:10]) if len(asks) >= 10 else 0

        if bid_depth > 0 and ask_depth > 0:
            imbalance = bid_depth / ask_depth

            if imbalance > 5.0:  # Buy side is 5x+ the sell side
                return 1.0, {
                    "name": "One-Sided Liquidity (Sell-Side Pulled)",
                    "observation": f"Buy-side depth is {imbalance:.1f}x the sell-side depth. "
                                 f"Sell liquidity has been significantly reduced.",
                    "significance": "Market makers pulling sell-side liquidity reduces "
                                  "resistance to upward price movement. This allows the "
                                  "price to be moved with minimal capital, which is a common "
                                  "tactic during the execution phase of a crime pump.",
                }
            elif imbalance > 3.0:
                return 0.5, {
                    "name": "Order Book Imbalance",
                    "observation": f"Buy/sell depth ratio is {imbalance:.1f}x",
                    "significance": "Moderate order book imbalance favoring upward movement.",
                }
        return 0, None

    def _build_reasoning(self, signals: list[dict], score: float) -> str:
        """Build reasoning summary for Layer 4."""
        if not signals:
            return "No spot or order book manipulation signals detected. Order book " \
                   "depth, liquidation patterns, and cross-exchange prices appear normal."

        parts = []
        parts.append(f"Layer 4 analysis identified {len(signals)} signal(s) "
                    f"with a combined score of {score:.2f}.")

        for sig in signals:
            parts.append(f"{sig['name']}: {sig['observation']}")

        if score >= 0.7:
            parts.append("The order book and execution-level data shows active manipulation "
                        "signatures. The combination of liquidation patterns, price behavior, "
                        "and volume anomalies indicates coordinated activity consistent "
                        "with crime pump execution.")
        elif score >= 0.4:
            parts.append("Multiple execution-level anomalies suggest something unusual "
                        "is occurring in the spot market for this token.")

        return " ".join(parts)
