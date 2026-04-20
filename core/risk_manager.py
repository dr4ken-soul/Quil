"""
Risk Manager

Dynamic risk adjustment system that adapts stop losses based on volatility,
applies correlation filters to prevent compounding losses, and detects the
current market regime to adjust strategy selection.

This module sits between signal generation and execution, applying final
risk checks before any order is placed.
"""

from typing import Optional
from datetime import datetime, timezone
from models.token import Token
from models.trade import TradeSignal, RiskParameters
from models import Direction, CrimeStage, Regime
from utils.logger import get_logger
from config.settings import (
    MAX_POSITION_SIZE_PCT,
    MAX_CORRELATED_POSITIONS,
    STOP_LOSS_BASE_PCT,
    STOP_LOSS_VOLATILITY_MULTIPLIER,
    MAX_DAILY_LOSS_PCT,
)

log = get_logger("risk_manager")


class RiskManager:
    """
    Applies risk management rules before trade execution.

    Features:
    - Dynamic stop loss tightening when volatility increases
    - Correlation filter preventing multiple highly-correlated positions
    - Regime detection adjusting strategy selection
    - Daily loss circuit breaker
    - Position sizing based on confidence and volatility
    """

    def __init__(self, portfolio_value_usd: float = 1000.0):
        self.portfolio_value = portfolio_value_usd
        self._open_positions: list[dict] = []
        self._daily_pnl: float = 0.0
        self._daily_pnl_reset: datetime = datetime.now(timezone.utc)
        self._trade_log: list[dict] = []

    def set_portfolio_value(self, value: float) -> None:
        """Update the portfolio value for position sizing calculations."""
        self.portfolio_value = value
        log.info("portfolio_updated", value=value)

    def validate_trade(self, signal: TradeSignal, token: Token) -> dict:
        """
        Run all risk checks on a trade signal before execution.

        Returns a dict with:
        - approved: bool
        - adjusted_signal: TradeSignal (with any risk adjustments applied)
        - rejections: list of reasons if not approved
        - warnings: list of non-blocking concerns
        """
        rejections = []
        warnings = []

        # Reset daily PnL if new day
        now = datetime.now(timezone.utc)
        if now.date() != self._daily_pnl_reset.date():
            self._daily_pnl = 0.0
            self._daily_pnl_reset = now

        # Check 1: Daily loss circuit breaker
        if self.portfolio_value > 0:
            daily_loss_pct = abs(self._daily_pnl) / self.portfolio_value * 100
            if self._daily_pnl < 0 and daily_loss_pct >= MAX_DAILY_LOSS_PCT:
                rejections.append(
                    f"Daily loss circuit breaker triggered. "
                    f"Loss of {daily_loss_pct:.1f}% exceeds {MAX_DAILY_LOSS_PCT}% limit."
                )

        # Check 2: Correlation filter
        correlated = self._check_correlation(token)
        if correlated >= MAX_CORRELATED_POSITIONS:
            rejections.append(
                f"Correlation filter: {correlated} correlated positions already open. "
                f"Max allowed is {MAX_CORRELATED_POSITIONS}."
            )

        # Check 3: Position size validation
        adjusted_risk = self._adjust_position_size(signal.risk, signal.confidence, token)
        signal.risk = adjusted_risk

        # Check 4: Dynamic stop loss adjustment
        signal.risk = self._adjust_stop_loss(signal.risk, token, signal.direction)

        # Check 5: Minimum confidence threshold
        if signal.confidence < 0.25:
            rejections.append(
                f"Confidence {signal.confidence:.0%} is below minimum 25% threshold."
            )

        # Check 6: Crime pump override warnings
        if signal.crime_pump_override:
            warnings.append(
                "Crime pump detected. Trade direction may conflict with manipulation flow. "
                "Position size has been reduced."
            )
            # Reduce position size for crime pump situations
            signal.risk.position_size_pct *= 0.5

        # Check 7: Liquidity check
        if token.metrics.liquidity_usd > 0 and self.portfolio_value > 0:
            trade_value = self.portfolio_value * signal.risk.position_size_pct / 100
            if trade_value > token.metrics.liquidity_usd * 0.02:
                warnings.append(
                    f"Trade size (${trade_value:,.0f}) exceeds 2% of available "
                    f"liquidity (${token.metrics.liquidity_usd:,.0f}). "
                    f"Expect significant slippage."
                )

        # Calculate max loss in USD
        if signal.risk.entry_price > 0 and signal.risk.stop_loss > 0:
            sl_distance_pct = abs(
                signal.risk.entry_price - signal.risk.stop_loss
            ) / signal.risk.entry_price
            trade_value = self.portfolio_value * signal.risk.position_size_pct / 100
            signal.risk.max_loss_usd = round(trade_value * sl_distance_pct, 2)

        approved = len(rejections) == 0

        if not approved:
            log.warning("trade_rejected", token=signal.token_symbol,
                       reasons=rejections)
        elif warnings:
            log.info("trade_approved_with_warnings", token=signal.token_symbol,
                    warnings=warnings)
        else:
            log.info("trade_approved", token=signal.token_symbol,
                    position_pct=signal.risk.position_size_pct)

        return {
            "approved": approved,
            "adjusted_signal": signal,
            "rejections": rejections,
            "warnings": warnings,
        }

    def _adjust_position_size(self, risk: RiskParameters,
                                confidence: float, token: Token) -> RiskParameters:
        """Adjust position size based on confidence, volatility, and portfolio."""
        # Base sizing from confidence
        base_pct = min(confidence * 8, MAX_POSITION_SIZE_PCT)

        # Reduce for high volatility
        change_1h = abs(token.metrics.price_change_1h)
        if change_1h > 10:
            base_pct *= 0.5
        elif change_1h > 5:
            base_pct *= 0.7

        # Reduce for low liquidity
        if token.metrics.liquidity_usd < 50_000:
            base_pct *= 0.5
        elif token.metrics.liquidity_usd < 200_000:
            base_pct *= 0.7

        risk.position_size_pct = round(max(base_pct, 0.5), 1)
        return risk

    def _adjust_stop_loss(self, risk: RiskParameters, token: Token,
                           direction: Direction) -> RiskParameters:
        """
        Dynamically tighten stop losses when volatility increases.
        Uses the volatility multiplier from config.
        """
        if risk.entry_price <= 0 or risk.stop_loss <= 0:
            return risk

        change_1h = abs(token.metrics.price_change_1h)

        # If volatility is high, tighten the stop
        if change_1h > 8:
            tightening_factor = 1 - (STOP_LOSS_VOLATILITY_MULTIPLIER * 0.1)
            current_sl_distance = abs(risk.entry_price - risk.stop_loss)
            new_sl_distance = current_sl_distance * tightening_factor

            if direction == Direction.LONG:
                risk.stop_loss = round(risk.entry_price - new_sl_distance, 8)
            else:
                risk.stop_loss = round(risk.entry_price + new_sl_distance, 8)

            log.info("stop_loss_tightened",
                    volatility=f"{change_1h:.1f}%",
                    new_sl=risk.stop_loss)

        return risk

    def _check_correlation(self, token: Token) -> int:
        """
        Check how many open positions are correlated with this token.
        Prevents entering multiple trades that would compound losses.
        """
        # Simple chain-based correlation for now
        correlated = 0
        for pos in self._open_positions:
            if pos.get("chain") == (token.chain.value if token.chain else None):
                correlated += 1
        return correlated

    def record_trade_open(self, signal: TradeSignal, token: Token) -> None:
        """Record an opened position for correlation tracking."""
        self._open_positions.append({
            "symbol": signal.token_symbol,
            "chain": token.chain.value if token.chain else None,
            "direction": signal.direction.value,
            "entry": signal.risk.entry_price,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        })

    def record_trade_close(self, symbol: str, pnl: float) -> None:
        """Record a closed position and update daily PnL."""
        self._open_positions = [
            p for p in self._open_positions if p["symbol"] != symbol
        ]
        self._daily_pnl += pnl
        self._trade_log.append({
            "symbol": symbol,
            "pnl": pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })

    def get_risk_summary(self) -> dict:
        """Get current risk state summary."""
        return {
            "portfolio_value": self.portfolio_value,
            "open_positions": len(self._open_positions),
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_pnl_pct": round(
                self._daily_pnl / self.portfolio_value * 100, 2
            ) if self.portfolio_value > 0 else 0,
            "circuit_breaker_active": (
                self._daily_pnl < 0 and
                abs(self._daily_pnl) / self.portfolio_value * 100 >= MAX_DAILY_LOSS_PCT
            ) if self.portfolio_value > 0 else False,
        }
