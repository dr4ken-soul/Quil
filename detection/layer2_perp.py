"""
Detection Layer 2: Perp Perspective and Index Manipulation

Analyzes derivatives market conditions including OI-to-market-cap ratio, volume anomalies,
price suppression followed by vertical moves, order book thinness, and cross-exchange
index weight exploitation.

Market makers influence mark price through exchanges that carry large weights in the
price index. When exchanges like Bitget or Gate carry significant index weight they become
easy targets for manipulation. Suspended deposits/withdrawals on these exchanges can
isolate the market and allow manipulation of Binance perp pricing from cheaper venues.
"""

from typing import Optional
from models.token import Token
from models.alert import DetectionLayerResult
from utils.logger import get_logger
from config.settings import (
    OI_TO_MCAP_HIGH_RATIO,
    VOLUME_TO_MCAP_SPIKE_RATIO,
    ORDER_BOOK_THIN_THRESHOLD,
    PRICE_SUPPRESSION_THEN_SPIKE_PCT,
)
from config.exchanges import EXCHANGE_INDEX_WEIGHTS, CRIME_PUMP_SPOT_EXCHANGES

log = get_logger("layer2_perp")


class PerpManipulationAnalyzer:
    """
    Detects perp market manipulation patterns used in crime pump operations.

    Signals monitored:
    - OI-to-market-cap ratio (primary crime coin indicator)
    - Fake OI detection (hedged positions inflating OI)
    - Volume-to-market-cap ratio spikes before pump
    - Price suppression followed by sudden vertical move
    - Order book thinness (few sell orders between current price and higher levels)
    - Low float + high holder concentration
    - Cross-exchange price/OI discrepancies
    - Exchange deposit/withdrawal suspensions
    - Four OI-price combinations (long/short opening/closing)
    """

    def __init__(self):
        self.layer_number = 2
        self.layer_name = "Perp/Index Manipulation"

    async def analyze(self, token: Token,
                       derivatives_data: dict = None) -> DetectionLayerResult:
        """
        Run all Layer 2 checks and return a scored result.
        """
        result = DetectionLayerResult(
            layer_number=self.layer_number,
            layer_name=self.layer_name,
            evaluable=(
                token.metrics.price > 0
                or token.metrics.market_cap > 0
                or token.metrics.volume_24h > 0
                or token.derivatives.open_interest_available
            ),
        )

        signals = []
        total_score = 0.0
        max_score = 7.0

        # ── Signal 1: OI to Market Cap Ratio ──────────────────────────────────
        score, signal = self._check_oi_to_mcap(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 2: Fake OI Detection ───────────────────────────────────────
        score, signal = self._check_fake_oi(token, derivatives_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 3: Volume to Market Cap Spike ──────────────────────────────
        score, signal = self._check_volume_to_mcap(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 4: Price Suppression then Spike ────────────────────────────
        score, signal = self._check_price_suppression_spike(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 5: Order Book Thinness ─────────────────────────────────────
        score, signal = self._check_order_book_depth(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 6: Cross-Exchange OI Concentration ─────────────────────────
        score, signal = self._check_exchange_oi_concentration(token, derivatives_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 7: Deposit/Withdrawal Suspension ───────────────────────────
        score, signal = self._check_deposit_withdrawal_suspension(token)
        if signal:
            signals.append(signal)
            total_score += score

        normalized_score = total_score / max_score if max_score > 0 else 0

        result.signals = signals
        result.score = round(normalized_score, 3)
        result.triggered = normalized_score >= 0.4
        result.reasoning = self._build_reasoning(signals, normalized_score, token)

        if result.triggered:
            log.info("layer2_triggered", token=token.symbol, score=result.score,
                    signals_count=len(signals))

        return result

    def _check_oi_to_mcap(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        OI-to-market-cap ratio is a primary crime coin indicator.
        High ratio significantly increases the probability of crime coin behavior.
        """
        ratio = token.derivatives.oi_to_mcap_ratio
        mcap = token.metrics.market_cap

        if mcap <= 0:
            return 0, None

        # Calculate from raw values if ratio not pre-computed
        if ratio <= 0 and token.derivatives.open_interest > 0:
            ratio = token.derivatives.open_interest / mcap

        if ratio >= OI_TO_MCAP_HIGH_RATIO:
            return 1.0, {
                "name": "High OI/Market Cap Ratio",
                "observation": f"OI-to-market-cap ratio is {ratio:.2f} "
                             f"(OI: ${token.derivatives.open_interest:,.0f}, "
                             f"MCap: ${mcap:,.0f})",
                "significance": "OI exceeding 50% of market cap is a primary crime coin indicator. "
                              "This level of derivatives exposure relative to spot market size "
                              "creates massive incentive for manipulation through liquidation "
                              "cascades and funding rate extraction.",
            }
        elif ratio >= OI_TO_MCAP_HIGH_RATIO * 0.6:
            return 0.5, {
                "name": "Elevated OI/Market Cap Ratio",
                "observation": f"OI-to-market-cap ratio is {ratio:.2f}",
                "significance": "Approaching the critical OI/mcap threshold. Derivatives "
                              "market is growing disproportionately to spot size.",
            }
        return 0, None

    def _check_fake_oi(self, token: Token,
                        derivatives_data: dict = None) -> tuple[float, Optional[dict]]:
        """
        Detect artificially inflated OI from market maker hedging.

        If OI increases or decreases significantly while the long/short ratio
        stays unchanged this indicates hedged positions created by market makers.
        In that case falling OI does not mean price will fall.
        """
        oi_change = token.derivatives.oi_change_4h
        ls_change = token.derivatives.long_short_ratio_change_4h
        ls_ratio = token.derivatives.long_short_ratio

        # Significant OI change with flat long/short ratio = hedged positions
        top_trader_change = token.derivatives.top_trader_long_short_ratio_change_4h
        top_traders_stable = (
            not token.derivatives.top_trader_ratio_history_available
            or abs(top_trader_change) < 2
        )
        if (token.derivatives.oi_history_available
                and token.derivatives.long_short_ratio_history_available
                and abs(oi_change) > 10 and abs(ls_change) < 2
                and top_traders_stable):
            top_trader_note = (
                f" Top-trader ratio moved {top_trader_change:+.1f}%"
                if token.derivatives.top_trader_ratio_history_available else ""
            )
            return 1.0, {
                "name": "Hedged OI Detected (Fake OI)",
                "observation": f"OI changed {oi_change:+.1f}% over 4h but long/short ratio moved only "
                             f"{ls_change:+.1f}% (current L/S: {ls_ratio:.2f}).{top_trader_note}",
                "significance": "OI is being inflated through market maker hedging. The stable "
                              "long/short ratio despite significant OI movement indicates positions "
                              "offset against each other. Falling OI in this context does not "
                              "indicate genuine selling pressure. The bot must never interpret "
                              "OI direction in isolation.",
            }
        return 0, None

    def _check_volume_to_mcap(self, token: Token) -> tuple[float, Optional[dict]]:
        """Check for abnormal volume relative to market cap in the hours before a pump."""
        mcap = token.metrics.market_cap
        vol_1h = token.metrics.volume_1h

        if mcap <= 0 or vol_1h <= 0:
            return 0, None

        ratio = vol_1h / mcap

        if ratio >= VOLUME_TO_MCAP_SPIKE_RATIO:
            return 1.0, {
                "name": "Volume/Market Cap Spike",
                "observation": f"1h volume ${vol_1h:,.0f} is {ratio:.1f}x the market cap "
                             f"${mcap:,.0f}",
                "significance": "Hourly trading volume exceeding the entire market cap indicates "
                              "abnormally aggressive trading activity. This is a common "
                              "pattern in the hours before crime pump execution.",
            }
        elif ratio >= VOLUME_TO_MCAP_SPIKE_RATIO * 0.5:
            return 0.5, {
                "name": "Elevated Volume/Market Cap",
                "observation": f"1h volume is {ratio:.2f}x market cap",
                "significance": "Volume is elevated relative to market cap size. "
                              "Not yet at critical levels but worth monitoring.",
            }
        return 0, None

    def _check_price_suppression_spike(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Detect price suppression followed by a sudden vertical move.
        Look for flat/declining price over a period followed by a sharp spike.
        """
        change_1h = abs(token.metrics.price_change_1h)
        change_24h = token.metrics.price_change_24h

        # Flat 24h but massive 1h move = suppression then spike
        if abs(change_24h) < 5 and change_1h >= PRICE_SUPPRESSION_THEN_SPIKE_PCT:
            return 1.0, {
                "name": "Price Suppression then Spike",
                "observation": f"Price was flat 24h ({change_24h:+.1f}%) then spiked "
                             f"{token.metrics.price_change_1h:+.1f}% in the last hour",
                "significance": "After a period of price suppression the price moved "
                              "vertically with sudden force. This matches the crime pump "
                              "pattern where market makers suppress price to attract shorts "
                              "then release it aggressively.",
            }
        elif change_1h >= PRICE_SUPPRESSION_THEN_SPIKE_PCT:
            return 0.5, {
                "name": "Aggressive Price Movement",
                "observation": f"Price moved {token.metrics.price_change_1h:+.1f}% in 1h",
                "significance": "Sharp price movement detected. Assessing whether this "
                              "follows a suppression pattern.",
            }
        return 0, None

    def _check_order_book_depth(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check for thin order books with few sell orders between current price
        and significantly higher levels.
        """
        book = token.order_book_data
        bids = book.get("bids", []) if book else []
        asks = book.get("asks", []) if book else []
        price = token.metrics.price
        mcap = token.metrics.market_cap
        if not asks or price <= 0 or mcap <= 0:
            return 0, None

        # Measure ask-side quote depth within 2% above market price. DEX
        # liquidity-to-market-cap is not an order-book measurement.
        ask_depth = sum(
            float(level[0]) * float(level[1])
            for level in asks
            if len(level) >= 2 and price <= float(level[0]) <= price * 1.02
        )
        if ask_depth <= 0:
            return 0, None
        depth_ratio = ask_depth / mcap

        if depth_ratio < ORDER_BOOK_THIN_THRESHOLD:
            return 1.0, {
                "name": "Thin Ask-Side Order Book",
                "observation": f"Ask depth within 2% of price is ${ask_depth:,.0f} "
                             f"({depth_ratio * 100:.2f}% of market cap)",
                "significance": "Measured sell-side depth is unusually small relative to "
                              "market capitalization, leaving less visible resistance to upward moves.",
            }
        return 0, None

    def _check_exchange_oi_concentration(self, token: Token,
                                          derivatives_data: dict = None) -> tuple[float, Optional[dict]]:
        """
        Check if OI is concentrated on exchanges with weak risk controls.
        High OI on Bitget, Gate, or Aster increases manipulation probability.
        """
        oi_dist = token.derivatives.exchange_oi_distribution
        if not oi_dist:
            return 0, None

        total_oi = sum(oi_dist.values())
        if total_oi <= 0:
            return 0, None

        weak_exchange_oi = sum(
            oi_dist.get(ex, 0) for ex in CRIME_PUMP_SPOT_EXCHANGES
        )
        weak_pct = weak_exchange_oi / total_oi

        if weak_pct >= 0.5:
            top_exchange = max(
                [(ex, oi_dist.get(ex, 0)) for ex in CRIME_PUMP_SPOT_EXCHANGES],
                key=lambda x: x[1]
            )
            return 1.0, {
                "name": "OI Concentrated on Weak-Control Exchanges",
                "observation": f"{weak_pct * 100:.0f}% of OI on exchanges with weak risk "
                             f"controls (primary: {top_exchange[0].title()})",
                "significance": "When OI concentrates on Bitget, Gate, or Aster the cost "
                              "of manipulation drops significantly. These exchanges have "
                              "weaker position limits and thinner order books, making them "
                              "the preferred venues for crime pump operations.",
            }
        return 0, None

    def _check_deposit_withdrawal_suspension(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check if any exchange has suspended deposits/withdrawals for this token.
        This can isolate the market and enable manipulation from cheaper venues.
        """
        suspensions = token.exchange_presence.deposit_withdrawal_suspended
        if not suspensions:
            return 0, None

        suspended_exchanges = [ex for ex, status in suspensions.items() if status]
        if suspended_exchanges:
            return 1.0, {
                "name": "Deposit/Withdrawal Suspended",
                "observation": f"Deposits/withdrawals suspended on: "
                             f"{', '.join(e.title() for e in suspended_exchanges)}",
                "significance": "Suspending deposits and withdrawals effectively isolates "
                              "that exchange's market. This allows manipulation of perp "
                              "pricing from a cheaper venue. When the suspended exchange "
                              "has significant index weight in mark price calculation "
                              "this becomes a critical manipulation enabler.",
            }
        return 0, None

    def classify_oi_price_action(self, oi_change: float,
                                  price_change: float) -> dict:
        """
        Classify the current OI-price combination into one of four states.

        The bot must read these four combinations:
        - Long opening: rising OI with rising price
        - Short opening: rising OI with falling price
        - Short closing: falling OI with rising price
        - Long closing: falling OI with falling price
        """
        if oi_change > 0 and price_change > 0:
            return {
                "state": "Long Opening",
                "description": "Rising OI with rising price. New long positions are being "
                             "opened, pushing the price higher.",
                "crime_pump_implication": "In Stage One this is expected as market makers "
                                        "push price up to attract short sellers.",
            }
        elif oi_change > 0 and price_change < 0:
            return {
                "state": "Short Opening",
                "description": "Rising OI with falling price. New short positions are being "
                             "opened, pushing the price lower.",
                "crime_pump_implication": "Shorts are entering the market. If this follows "
                                        "a Stage One pump it may indicate the trap is working.",
            }
        elif oi_change < 0 and price_change > 0:
            return {
                "state": "Short Closing",
                "description": "Falling OI with rising price. Short positions are being "
                             "closed (likely liquidated), driving price higher.",
                "crime_pump_implication": "Short squeeze in progress. This is the execution "
                                        "phase of Stage Three where trapped shorts are being "
                                        "liquidated.",
            }
        elif oi_change < 0 and price_change < 0:
            return {
                "state": "Long Closing",
                "description": "Falling OI with falling price. Long positions are being "
                             "closed, allowing the price to drop.",
                "crime_pump_implication": "In Stage Four this indicates distribution. Market "
                                        "makers closing long positions and selling spot holdings.",
            }
        else:
            return {
                "state": "Neutral",
                "description": "Minimal change in both OI and price.",
                "crime_pump_implication": "No clear directional signal at this time.",
            }

    def _build_reasoning(self, signals: list[dict], score: float,
                          token: Token) -> str:
        """Build reasoning summary for Layer 2."""
        if not signals:
            return "No perp/index manipulation signals detected. OI, volume, and order book " \
                   "conditions appear within normal ranges across all monitored exchanges."

        parts = []
        parts.append(f"Layer 2 analysis identified {len(signals)} signal(s) "
                    f"with a combined score of {score:.2f}.")

        for sig in signals:
            parts.append(f"{sig['name']}: {sig['observation']}")

        # Add OI-price classification
        oi_price = self.classify_oi_price_action(
            token.derivatives.oi_change_1h,
            token.metrics.price_change_1h
        )
        parts.append(f"Current OI-Price state: {oi_price['state']}. "
                    f"{oi_price['crime_pump_implication']}")

        if score >= 0.7:
            parts.append("Derivatives market structure shows strong signs of manipulation "
                        "preparation. The combination of signals suggests active market "
                        "maker positioning ahead of a potential forced move.")
        elif score >= 0.4:
            parts.append("Multiple derivatives anomalies detected. The market structure "
                        "warrants close monitoring for escalation.")

        return " ".join(parts)
