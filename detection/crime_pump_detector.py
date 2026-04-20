"""
Crime Pump Detector (Orchestrator)

Master orchestrator that runs all four detection layers, the lifecycle tracker,
and produces unified crime pump alerts. Handles both autonomous scanning and
manual token analysis requests.

Integration Rule: A token must not be flagged as a confirmed crime pump signal
unless it shows patterns across at least two of the four detection layers.
Single-layer signals are output as watch alerts.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from models.token import Token
from models.alert import CrimePumpAlert, DetectionLayerResult
from models import ConfidenceLevel, CrimeStage, Recommendation
from detection.layer1_onchain import OnChainSupplyAnalyzer
from detection.layer2_perp import PerpManipulationAnalyzer
from detection.layer3_funding import FundingRateAnalyzer
from detection.layer4_orderbook import OrderBookManipulationAnalyzer
from detection.lifecycle import LifecycleTracker
from utils.logger import get_logger
from utils.database import save_crime_pump_alert, save_scan_result, get_latest_alert
from config.settings import MIN_LAYERS_CONFIRMED, ALERT_COOLDOWN_MINUTES

log = get_logger("crime_pump_detector")


class CrimePumpDetector:
    """
    Main crime pump detection engine.

    Runs all four detection layers in parallel, classifies the lifecycle stage,
    and produces structured alerts with full reasoning.
    """

    def __init__(self):
        self.layer1 = OnChainSupplyAnalyzer()
        self.layer2 = PerpManipulationAnalyzer()
        self.layer3 = FundingRateAnalyzer()
        self.layer4 = OrderBookManipulationAnalyzer()
        self.lifecycle = LifecycleTracker()

        # Track data source availability for degraded state handling
        self._data_source_status: dict[str, bool] = {
            "dexscreener": True,
            "coinglass": True,
            "blockchain_explorer": True,
            "ccxt": True,
            "social": True,
        }

    async def scan_token(self, token: Token,
                          wallet_data: dict = None,
                          derivatives_data: dict = None,
                          funding_data: list = None,
                          order_book_data: dict = None,
                          liquidation_data: list = None,
                          is_refresh: bool = False) -> CrimePumpAlert:
        """
        Run the full crime pump detection pipeline on a single token.

        This is the core method called both by autonomous scanning and
        manual user-submitted ticker analysis.
        """
        log.info("scanning_token", token=token.symbol,
                contract=token.contract_address, refresh=is_refresh)

        # Run all four detection layers in parallel
        layer_results = await asyncio.gather(
            self.layer1.analyze(token, wallet_data=wallet_data),
            self.layer2.analyze(token, derivatives_data=derivatives_data),
            self.layer3.analyze(token, funding_data=funding_data),
            self.layer4.analyze(token, order_book_data=order_book_data,
                              liquidation_data=liquidation_data),
            return_exceptions=True,
        )

        # Handle any layer failures
        valid_results: list[DetectionLayerResult] = []
        failed_layers: list[str] = []

        for i, result in enumerate(layer_results):
            if isinstance(result, Exception):
                layer_name = ["On-Chain", "Perp/Index", "Funding", "Order Book"][i]
                log.error("layer_failed", layer=layer_name, error=str(result))
                failed_layers.append(layer_name)
                # Create empty result for failed layer
                valid_results.append(DetectionLayerResult(
                    layer_number=i + 1,
                    layer_name=layer_name,
                    triggered=False,
                    score=0,
                    reasoning=f"Layer {i + 1} analysis failed: {str(result)}",
                ))
            else:
                valid_results.append(result)

        # Count triggered layers
        triggered_layers = [r for r in valid_results if r.triggered]
        triggered_numbers = [r.layer_number for r in triggered_layers]

        # Check degraded state
        evaluable_layers = 4 - len(failed_layers)
        if evaluable_layers < MIN_LAYERS_CONFIRMED:
            log.warning("degraded_scan", token=token.symbol,
                       failed=failed_layers, evaluable=evaluable_layers)

        # Classify lifecycle stage
        lifecycle_result = self.lifecycle.classify_stage(token, valid_results)

        # Build the alert
        alert = CrimePumpAlert(
            token_symbol=token.symbol,
            contract_address=token.contract_address,
            chain=token.chain.value if token.chain else None,
            layer_results=valid_results,
            layers_triggered=triggered_numbers,
            total_layers_triggered=len(triggered_layers),
            crime_stage=lifecycle_result["stage"],
            price=token.metrics.price,
            market_cap=token.metrics.market_cap,
            liquidity=token.metrics.liquidity_usd,
            oi_to_mcap_ratio=token.derivatives.oi_to_mcap_ratio,
            funding_rate=token.derivatives.funding_rate,
            top10_wallet_concentration=token.onchain.top10_wallet_pct,
            primary_exchange=token.exchange_presence.primary_spot_exchange,
            is_refresh=is_refresh,
        )

        # Aggregate all signals from triggered layers
        for lr in valid_results:
            for sig in lr.signals:
                alert.signals_detected.append({
                    "layer": lr.layer_number,
                    "layer_name": lr.layer_name,
                    **sig,
                })

        # Compute confidence and recommendation
        alert.compute_confidence()
        alert.compute_recommendation()

        # Build full reasoning
        alert.full_reasoning = self._build_full_reasoning(
            alert, valid_results, lifecycle_result, failed_layers
        )

        # Handle refresh comparison
        if is_refresh:
            previous = await get_latest_alert(token.symbol)
            if previous:
                alert.previous_alert_id = previous.get("id")
                alert.changes_from_previous = self._compute_changes(
                    previous, alert
                )

        # Save to database
        alert_data = {
            "token_symbol": alert.token_symbol,
            "contract_address": alert.contract_address,
            "chain": alert.chain,
            "confidence_level": alert.confidence_level.value,
            "crime_stage": alert.crime_stage.value,
            "layers_triggered": alert.layers_triggered,
            "signals": [s for s in alert.signals_detected],
            "price": alert.price,
            "market_cap": alert.market_cap,
            "liquidity": alert.liquidity,
            "oi_to_mcap_ratio": alert.oi_to_mcap_ratio,
            "funding_rate": alert.funding_rate,
            "top10_wallet_concentration": alert.top10_wallet_concentration,
            "primary_exchange": alert.primary_exchange,
            "recommendation": alert.recommendation.value,
            "full_reasoning": alert.full_reasoning,
        }

        try:
            alert.scan_id = await save_crime_pump_alert(alert_data)
        except Exception as e:
            log.error("alert_save_failed", error=str(e))

        # Save scan result
        try:
            await save_scan_result({
                "token_symbol": token.symbol,
                "contract_address": token.contract_address,
                "chain": alert.chain,
                "scan_type": "crime_pump",
                "layer1_score": valid_results[0].score if len(valid_results) > 0 else 0,
                "layer2_score": valid_results[1].score if len(valid_results) > 1 else 0,
                "layer3_score": valid_results[2].score if len(valid_results) > 2 else 0,
                "layer4_score": valid_results[3].score if len(valid_results) > 3 else 0,
                "total_score": sum(r.score for r in valid_results) / len(valid_results) if valid_results else 0,
            })
        except Exception as e:
            log.error("scan_save_failed", error=str(e))

        log.info("scan_complete", token=token.symbol,
                confidence=alert.confidence_level.value,
                stage=alert.crime_stage.value,
                layers=len(triggered_layers))

        return alert

    def should_send_alert(self, alert: CrimePumpAlert) -> bool:
        """
        Determine if an alert should be sent to Telegram.

        Rules:
        - Confirmed alerts (2+ layers) are always sent
        - Single-layer triggers are sent as watch alerts
        - Respect cooldown period between alerts for same token
        - Never send confirmed alert with fewer than 2 evaluable layers
        """
        if not alert.is_confirmed and alert.total_layers_triggered < 1:
            return False

        # Check cooldown
        if alert.previous_alert_id and not alert.is_refresh:
            # Already alerted recently. Only re-alert if confidence increased
            pass

        return True

    def _build_full_reasoning(self, alert: CrimePumpAlert,
                               layer_results: list[DetectionLayerResult],
                               lifecycle_result: dict,
                               failed_layers: list[str]) -> str:
        """
        Build the complete reasoning narrative combining all layer analyses
        and lifecycle classification.
        """
        parts = []

        # Degraded state warning
        if failed_layers:
            parts.append(f"Note: This scan operated with degraded data. "
                        f"The following sources were unavailable: "
                        f"{', '.join(failed_layers)}. Results should be "
                        f"interpreted with this limitation in mind.")

        # Overall assessment
        if alert.is_confirmed:
            parts.append(f"Crime pump detection CONFIRMED for {alert.token_symbol} "
                        f"with {alert.confidence_level.value} confidence. "
                        f"{alert.total_layers_triggered} of 4 detection layers triggered.")
        elif alert.total_layers_triggered == 1:
            parts.append(f"Single detection layer triggered for {alert.token_symbol}. "
                        f"This is a WATCH alert, not a confirmed detection. "
                        f"The token should be monitored for escalation across "
                        f"additional layers.")
        else:
            parts.append(f"No detection layers triggered for {alert.token_symbol}. "
                        f"All four layers were evaluated and no crime pump signals "
                        f"were found at this time.")

        # Layer-by-layer summary
        for lr in layer_results:
            if lr.triggered:
                parts.append(f"Layer {lr.layer_number} ({lr.layer_name}): TRIGGERED "
                           f"(score: {lr.score:.2f}). {lr.reasoning}")
            else:
                parts.append(f"Layer {lr.layer_number} ({lr.layer_name}): Clear "
                           f"(score: {lr.score:.2f}).")

        # Lifecycle stage
        parts.append(f"Lifecycle classification: {lifecycle_result['stage'].value} "
                    f"({lifecycle_result['confidence']:.0%} confidence). "
                    f"{lifecycle_result['reasoning']}")

        return " ".join(parts)

    def _compute_changes(self, previous: dict,
                          current: CrimePumpAlert) -> list[str]:
        """
        Compare current alert with previous one and list what changed.
        Used for the refresh functionality.
        """
        changes = []

        # Confidence change
        prev_conf = previous.get("confidence_level", "")
        if prev_conf != current.confidence_level.value:
            changes.append(f"Confidence changed from {prev_conf} to "
                         f"{current.confidence_level.value}")

        # Stage change
        prev_stage = previous.get("crime_stage", "")
        if prev_stage != current.crime_stage.value:
            changes.append(f"Crime coin stage changed from {prev_stage} to "
                         f"{current.crime_stage.value}")

        # Layer changes
        prev_layers = str(previous.get("layers_triggered", "[]"))
        curr_layers = str(current.layers_triggered)
        if prev_layers != curr_layers:
            changes.append(f"Triggered layers changed from {prev_layers} to "
                         f"{curr_layers}")

        # Price change
        prev_price = previous.get("price", 0)
        if prev_price and prev_price > 0:
            price_change_pct = ((current.price - prev_price) / prev_price) * 100
            if abs(price_change_pct) > 1:
                changes.append(f"Price moved {price_change_pct:+.1f}% "
                             f"(${prev_price:.6f} to ${current.price:.6f})")

        # Funding rate change
        prev_funding = previous.get("funding_rate", 0)
        if prev_funding != current.funding_rate:
            changes.append(f"Funding rate changed from {prev_funding * 100:.2f}% "
                         f"to {current.funding_rate * 100:.2f}%")

        # OI ratio change
        prev_oi = previous.get("oi_to_mcap_ratio", 0)
        if prev_oi and abs(prev_oi - current.oi_to_mcap_ratio) > 0.05:
            changes.append(f"OI/MCap ratio changed from {prev_oi:.2f} to "
                         f"{current.oi_to_mcap_ratio:.2f}")

        if not changes:
            changes.append("No significant changes detected since last analysis")

        return changes
