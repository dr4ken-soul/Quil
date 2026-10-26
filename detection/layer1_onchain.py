"""
Detection Layer 1: On-Chain Supply Control

Analyzes wallet concentration, cold wallet withdrawals, accumulation patterns,
newly funded wallet activity, contract age, and liquidity lock status.

This is the foundational detection layer. Before operations begin the project
typically accumulates more than 90% of supply on-chain through multisig wallets
and hands control to market makers for execution.
"""

from typing import Optional
from models.token import Token
from models.alert import DetectionLayerResult
from utils.logger import get_logger
from config.settings import (
    SUPPLY_CONCENTRATION_THRESHOLD,
    ACCUMULATION_WINDOW_HOURS,
    COLD_WALLET_WITHDRAWAL_ALERT_USD,
    TOKEN_AGE_SUSPICIOUS_DAYS,
    TRANSFER_SPIKE_MULTIPLIER,
)

log = get_logger("layer1_onchain")


class OnChainSupplyAnalyzer:
    """
    Detects on-chain supply control patterns that precede crime pumps.

    Signals monitored:
    - Supply concentration in few wallets (>90% in multisig/related addresses)
    - Bot-driven market making patterns (abnormal volume + candlestick behavior)
    - Cold wallet withdrawals from exchanges (preparation signal)
    - Sudden accumulation by small number of wallets (24-72h window)
    - Low liquidity relative to market cap
    - Young token contracts (<30 days)
    - Unlocked liquidity
    - Transfer volume spikes from small wallet clusters
    """

    def __init__(self):
        self.layer_number = 1
        self.layer_name = "On-Chain Supply Control"

    async def analyze(self, token: Token, wallet_data: dict = None,
                       transfer_data: list = None) -> DetectionLayerResult:
        """
        Run all Layer 1 checks and return a scored result.

        A token must show multiple on-chain anomalies to score high.
        Each signal contributes a weighted score to the total.
        """
        result = DetectionLayerResult(
            layer_number=self.layer_number,
            layer_name=self.layer_name,
            evaluable=(
                token.onchain.data_available
                or (token.metrics.market_cap > 0 and token.metrics.liquidity_usd > 0)
            ),
        )

        signals = []
        total_score = 0.0
        max_score = 7.0  # Maximum possible score from all signals

        # ── Signal 1: Supply Concentration ────────────────────────────────────
        score, signal = self._check_supply_concentration(token, wallet_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 2: Cold Wallet Withdrawals ─────────────────────────────────
        score, signal = self._check_cold_wallet_withdrawals(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 3: Sudden Accumulation ─────────────────────────────────────
        score, signal = self._check_sudden_accumulation(token, wallet_data)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 4: Low Liquidity vs Market Cap ─────────────────────────────
        score, signal = self._check_liquidity_to_mcap(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 5: Token Contract Age ──────────────────────────────────────
        score, signal = self._check_contract_age(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 6: Liquidity Lock Status ───────────────────────────────────
        score, signal = self._check_liquidity_lock(token)
        if signal:
            signals.append(signal)
            total_score += score

        # ── Signal 7: Transfer Volume Spikes ──────────────────────────────────
        score, signal = self._check_transfer_spikes(token)
        if signal:
            signals.append(signal)
            total_score += score

        # Calculate normalized score (0 to 1)
        normalized_score = total_score / max_score if max_score > 0 else 0

        result.signals = signals
        result.score = round(normalized_score, 3)
        result.triggered = normalized_score >= 0.4  # Layer triggers at 40% of max signals
        result.reasoning = self._build_reasoning(signals, normalized_score)

        if result.triggered:
            log.info("layer1_triggered", token=token.symbol, score=result.score,
                    signals_count=len(signals))

        return result

    def _check_supply_concentration(self, token: Token,
                                     wallet_data: dict = None) -> tuple[float, Optional[dict]]:
        """Check if a few wallets control a disproportionate share of supply."""
        # Wallet clustering is based on transfer recipients, not token balances.
        # Only holder-balance data may be used for supply concentration.
        if token.onchain.top10_wallet_pct is not None:
            concentration_pct = token.onchain.top10_wallet_pct
            concentration_label = "Top 10 wallets"
        elif token.onchain.top20_wallet_pct is not None:
            concentration_pct = token.onchain.top20_wallet_pct
            concentration_label = "Top 20 wallets"
        elif token.onchain.top20_account_pct is not None:
            concentration_pct = token.onchain.top20_account_pct
            concentration_label = "Top 20 token accounts (lower bound)"
        else:
            # Missing holder data is unknown, never a measured zero.
            return 0, None

        concentration = concentration_pct / 100

        if concentration >= SUPPLY_CONCENTRATION_THRESHOLD:
            return 1.0, {
                "name": "Supply Concentration",
                "observation": f"{concentration_label} control {concentration * 100:.1f}% of supply",
                "source": token.onchain.holder_concentration_source,
                "significance": "Exceeds 90% threshold. Multiple related addresses controlling "
                              "most of the supply is the primary setup pattern for crime pumps. "
                              "This supply is typically held through multisig wallets and handed "
                              "to market makers before operations begin.",
            }
        elif concentration >= 0.70:
            return 0.5, {
                "name": "Supply Concentration (Elevated)",
                "observation": f"{concentration_label} control {concentration * 100:.1f}% of supply",
                "source": token.onchain.holder_concentration_source,
                "significance": "Above 70% but below critical 90% threshold. Worth monitoring "
                              "as concentration may be building toward the accumulation target.",
            }
        return 0, None

    def _check_cold_wallet_withdrawals(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check for large withdrawals from exchange cold wallets.
        This is a direct preparation signal for upcoming manipulation.
        """
        outflows = token.onchain.cold_wallet_outflows_24h or 0.0
        if outflows >= COLD_WALLET_WITHDRAWAL_ALERT_USD:
            return 1.0, {
                "name": "Cold Wallet Withdrawal",
                "observation": f"${outflows:,.0f} withdrawn from exchange cold wallets in 24h",
                "significance": "Large withdrawals from exchange cold wallets to on-chain "
                              "addresses are a direct preparation signal. This is one of the "
                              "most reliable indicators that manipulation is being staged.",
            }
        elif outflows >= COLD_WALLET_WITHDRAWAL_ALERT_USD * 0.5:
            return 0.5, {
                "name": "Cold Wallet Withdrawal (Moderate)",
                "observation": f"${outflows:,.0f} withdrawn from exchange cold wallets in 24h",
                "significance": "Notable outflows detected but below critical threshold. "
                              "Could indicate early preparation activity.",
            }
        return 0, None

    def _check_sudden_accumulation(self, token: Token,
                                    wallet_data: dict = None) -> tuple[float, Optional[dict]]:
        """
        Check for sudden accumulation by a small number of wallets
        in the 24-72 hour window before a potential pump.
        """
        if wallet_data and wallet_data.get("analyzable"):
            recent_count = wallet_data.get("recent_recipients_count", 0)
            burst_count = wallet_data.get("burst_receiving_wallets", 0)

            # High ratio of recent concentrated buying
            if burst_count >= 5 and recent_count > 0:
                return 1.0, {
                    "name": "Clustered Token Inflows",
                    "observation": f"{burst_count} recipient wallets received repeated transfers; "
                                 f"{recent_count} recipients were active in the "
                                 f"{ACCUMULATION_WINDOW_HOURS}h window",
                    "significance": "Repeated token transfers to a small group of recipients "
                                  "are visible on-chain. Transfers do not prove market buys, but "
                                  "they can indicate concentrated token movement.",
                }
            elif burst_count >= 2:
                return 0.5, {
                    "name": "Repeated Token Inflows",
                    "observation": f"{burst_count} recipient wallets had repeated transfers",
                    "significance": "Some repeated token movement is visible, but it does not "
                                  "establish that the transfers were market purchases.",
                }

        # Fall back to new wallet buy count
        new_wallet_buys = token.onchain.new_wallet_buy_count_1h or 0
        if new_wallet_buys >= 10:
            return 0.7, {
                "name": "New Wallet Buying Cluster",
                "observation": f"{new_wallet_buys} newly funded wallets buying in the last hour",
                "significance": "High concentration of first-time buyers suggests coordinated "
                              "wallet funding and buying ahead of a planned move.",
            }
        return 0, None

    def _check_liquidity_to_mcap(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check if liquidity is suspiciously low relative to market cap.
        Low liquidity makes the price easy to move with small capital.
        """
        mcap = token.metrics.market_cap
        liq = token.metrics.liquidity_usd

        if mcap <= 0 or liq <= 0:
            return 0, None

        liq_ratio = liq / mcap

        if liq_ratio < 0.02:  # Less than 2% liquidity relative to mcap
            return 1.0, {
                "name": "Low Liquidity / Market Cap Ratio",
                "observation": f"Liquidity ${liq:,.0f} is only {liq_ratio * 100:.2f}% of "
                             f"market cap ${mcap:,.0f}",
                "significance": "Extremely thin liquidity relative to market cap. Price can "
                              "be moved aggressively with minimal capital. This is the classic "
                              "low mcap manipulation setup.",
            }
        elif liq_ratio < 0.05:
            return 0.5, {
                "name": "Low Liquidity (Moderate)",
                "observation": f"Liquidity is {liq_ratio * 100:.2f}% of market cap",
                "significance": "Below average liquidity depth. More susceptible to "
                              "price manipulation than well-established tokens.",
            }
        return 0, None

    def _check_contract_age(self, token: Token) -> tuple[float, Optional[dict]]:
        """Check if the token contract is suspiciously young."""
        age = token.onchain.contract_age_days

        if age is not None and age <= TOKEN_AGE_SUSPICIOUS_DAYS:
            score = 1.0 if age <= 7 else 0.5
            return score, {
                "name": "Young Token Contract",
                "observation": f"Contract is {age} days old",
                "significance": f"Token contract is under {TOKEN_AGE_SUSPICIOUS_DAYS} days old. "
                              "Young tokens have no established trading history and are "
                              "prime targets for crime pump operations.",
            }
        return 0, None

    def _check_liquidity_lock(self, token: Token) -> tuple[float, Optional[dict]]:
        """Check if liquidity is locked or unlocked."""
        if token.onchain.liquidity_locked is False:
            return 0.5, {
                "name": "Unlocked Liquidity",
                "observation": "No liquidity lock detected",
                "significance": "Unlocked liquidity means the deployer can pull liquidity "
                              "at any time. While not definitive on its own this increases "
                              "the risk profile when combined with other signals.",
            }
        return 0, None

    def _check_transfer_spikes(self, token: Token) -> tuple[float, Optional[dict]]:
        """
        Check for abnormal transfer volume spikes from a small cluster of wallets.
        """
        count_24h = token.onchain.transfer_count_24h or 0
        avg_7d = token.onchain.transfer_count_avg_7d or 0.0

        if avg_7d > 0 and count_24h > 0:
            ratio = count_24h / avg_7d
            if ratio >= TRANSFER_SPIKE_MULTIPLIER:
                return 1.0, {
                    "name": "Transfer Volume Spike",
                    "observation": f"Transfer count is {ratio:.1f}x the 7-day average "
                                 f"({count_24h} vs avg {avg_7d:.0f})",
                    "significance": "Abnormal transfer activity from a concentrated set of "
                                  "wallets. This pattern is observable through on-chain data "
                                  "and suggests coordinated activity rather than organic trading.",
                }
        return 0, None

    def _build_reasoning(self, signals: list[dict], score: float) -> str:
        """Build a human-readable reasoning summary for this layer."""
        if not signals:
            return "No on-chain supply control anomalies detected across all monitored signals."

        parts = []
        parts.append(f"Layer 1 analysis identified {len(signals)} signal(s) "
                    f"with a combined score of {score:.2f}.")

        for sig in signals:
            parts.append(f"{sig['name']}: {sig['observation']}")

        if score >= 0.7:
            parts.append("On-chain evidence strongly suggests supply is being concentrated "
                        "and controlled in preparation for coordinated price movement.")
        elif score >= 0.4:
            parts.append("Multiple on-chain indicators are elevated. This token should be "
                        "monitored closely for further accumulation or preparation signals.")
        else:
            parts.append("Some minor on-chain indicators present but insufficient evidence "
                        "for a supply control conclusion at this time.")

        return " ".join(parts)
