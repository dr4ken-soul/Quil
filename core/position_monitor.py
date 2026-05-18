"""
Position Monitor

Tracks open positions and sends real-time Telegram alerts when:
- Take profit levels are hit
- Stop loss is hit
- Liquidation occurs
- Custom profit thresholds are reached (50%, 100%, 200%, x2, x5, x10)
- Analysis target prices are reached

Runs as a background task checking prices every 5 seconds.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from utils.logger import get_logger
from utils.database import save_position, get_active_positions, update_position, close_position

log = get_logger("position_monitor")


class AlertType(str, Enum):
    TP1_HIT = "TP1 Hit"
    TP2_HIT = "TP2 Hit"
    SL_HIT = "Stop Loss Hit"
    LIQUIDATED = "Liquidated"
    PROFIT_50 = "50% Profit"
    PROFIT_100 = "100% Profit"
    PROFIT_200 = "200% Profit"
    PROFIT_X2 = "x2 Profit"
    PROFIT_X5 = "x5 Profit"
    PROFIT_X10 = "x10 Profit"
    CUSTOM = "Custom Alert"
    LOSS_25 = "25% Loss"
    LOSS_50 = "50% Loss"


# Default profit alert thresholds (percentage gain from entry)
DEFAULT_PROFIT_ALERTS = [50, 100, 200]
# Default multiplier alerts
DEFAULT_MULTIPLIER_ALERTS = [2, 5, 10]


class TrackedPosition:
    """Represents a position being monitored for alerts."""

    def __init__(
        self,
        position_id: int,
        symbol: str,
        direction: str,  # LONG or SHORT
        entry_price: float,
        stop_loss: float = 0.0,
        take_profit_1: float = 0.0,
        take_profit_2: float = 0.0,
        leverage: int = 1,
        size_usd: float = 0.0,
        liquidation_price: float = 0.0,
        profit_alert_pcts: list[float] = None,
        multiplier_alerts: list[float] = None,
        custom_price_alerts: list[float] = None,
    ):
        self.position_id = position_id
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.current_price = entry_price
        self.stop_loss = stop_loss
        self.original_stop_loss = stop_loss  # Never modified — used for real SL checks
        self.take_profit_1 = take_profit_1
        self.take_profit_2 = take_profit_2
        self.leverage = leverage
        self.size_usd = size_usd  # This is MARGIN amount (what user risked)
        self.liquidation_price = liquidation_price or self._calc_liquidation_price()

        # Alert thresholds
        self.profit_alert_pcts = profit_alert_pcts or DEFAULT_PROFIT_ALERTS.copy()
        self.multiplier_alerts = multiplier_alerts or DEFAULT_MULTIPLIER_ALERTS.copy()
        self.custom_price_alerts = custom_price_alerts or []

        # Track which alerts have already fired (don't send duplicates)
        self.fired_alerts: set[str] = set()

        # State
        self.is_open = True
        self.opened_at = datetime.now(timezone.utc)
        self.closed_at: Optional[datetime] = None
        self.close_reason: Optional[str] = None
        self.realized_pnl: float = 0.0

    def _calc_liquidation_price(self) -> float:
        """Estimate liquidation price based on leverage (Bybit-style formula).
        Uses 2.5% maintenance margin rate which matches Bybit's rate for
        small-cap and Innovation Zone altcoins. BTC/ETH use lower rates,
        but since the user primarily trades altcoins, 2.5% is more accurate.
        """
        if self.leverage <= 1 or self.entry_price <= 0:
            return 0.0

        # Bybit maintenance margin rate for small-cap/Innovation Zone altcoins
        maintenance_margin_rate = 0.025  # 2.5%
        taker_fee = 0.0006  # 0.06%

        initial_margin_rate = 1.0 / self.leverage

        if self.direction == "LONG":
            liq = self.entry_price * (1 - initial_margin_rate + maintenance_margin_rate + taker_fee)
            return max(liq, 0)
        else:
            liq = self.entry_price * (1 + initial_margin_rate - maintenance_margin_rate - taker_fee)
            return liq

    @property
    def pnl_pct(self) -> float:
        """Current PnL as percentage (including leverage)."""
        if self.entry_price <= 0:
            return 0.0
        if self.direction == "LONG":
            raw_pct = (self.current_price - self.entry_price) / self.entry_price * 100
        else:
            raw_pct = (self.entry_price - self.current_price) / self.entry_price * 100
        return raw_pct * self.leverage

    @property
    def pnl_usd(self) -> float:
        """Current PnL in USD (margin-based, leverage already in pnl_pct)."""
        if self.size_usd <= 0:
            return 0.0
        # size_usd = margin (what user risked). pnl_pct includes leverage.
        return self.size_usd * self.pnl_pct / 100

    @property
    def current_multiplier(self) -> float:
        """Current profit as multiplier (e.g., 2.0 = x2)."""
        if self.entry_price <= 0:
            return 1.0
        if self.direction == "LONG":
            return self.current_price / self.entry_price
        else:
            return (2 * self.entry_price - self.current_price) / self.entry_price

    def check_alerts(self, current_price: float) -> list[dict]:
        """
        Check all alert conditions against the current price.
        Returns a list of triggered alerts.
        """
        self.current_price = current_price
        triggered = []

        if not self.is_open:
            return triggered

        # Check TP1 (partial profit alert, position stays open)
        if self.take_profit_1 > 0 and "TP1" not in self.fired_alerts:
            if self._price_crossed(self.take_profit_1):
                triggered.append(self._make_alert(
                    AlertType.TP1_HIT, self.take_profit_1,
                    extra="Consider taking partial profits and moving SL to breakeven."
                ))
                self.fired_alerts.add("TP1")

        # Check TP2 (final target — AUTO-CLOSE the position)
        if self.take_profit_2 > 0 and "TP2" not in self.fired_alerts:
            if self._price_crossed(self.take_profit_2):
                triggered.append(self._make_alert(
                    AlertType.TP2_HIT, self.take_profit_2,
                    extra="Final take profit hit. Position auto-closed."
                ))
                self.fired_alerts.add("TP2")
                self.is_open = False
                self.close_reason = "Take Profit 2 Hit"
                self.closed_at = datetime.now(timezone.utc)
                return triggered  # Position closed — no more alerts

        # Check Stop Loss — ONLY against the original SL (never moved by trailing)
        if self.original_stop_loss > 0 and "SL" not in self.fired_alerts:
            if self._price_hit_original_sl():
                triggered.append(self._make_alert(AlertType.SL_HIT, self.original_stop_loss))
                self.fired_alerts.add("SL")
                self.is_open = False
                self.close_reason = "Stop Loss Hit"
                self.closed_at = datetime.now(timezone.utc)
                return triggered  # Position closed — no more alerts

        # Check Liquidation
        if self.liquidation_price > 0 and "LIQUIDATED" not in self.fired_alerts:
            if self._price_hit_liquidation():
                triggered.append(self._make_alert(AlertType.LIQUIDATED, self.liquidation_price))
                self.fired_alerts.add("LIQUIDATED")
                self.is_open = False
                self.close_reason = "Liquidated"
                self.closed_at = datetime.now(timezone.utc)
                return triggered  # Position closed — no more alerts

        # Check profit percentage alerts
        current_pnl = self.pnl_pct
        for pct in self.profit_alert_pcts:
            alert_key = f"PROFIT_{pct}"
            if alert_key not in self.fired_alerts and current_pnl >= pct:
                alert_type = {
                    50: AlertType.PROFIT_50,
                    100: AlertType.PROFIT_100,
                    200: AlertType.PROFIT_200,
                }.get(pct, AlertType.CUSTOM)
                triggered.append(self._make_alert(
                    alert_type, current_price,
                    extra=f"Position is up {current_pnl:.1f}% ({pct}% threshold hit)"
                ))
                self.fired_alerts.add(alert_key)

        # Check multiplier alerts
        current_mult = self.current_multiplier
        for mult in self.multiplier_alerts:
            alert_key = f"MULT_{mult}x"
            if alert_key not in self.fired_alerts and current_mult >= mult:
                alert_type = {
                    2: AlertType.PROFIT_X2,
                    5: AlertType.PROFIT_X5,
                    10: AlertType.PROFIT_X10,
                }.get(mult, AlertType.CUSTOM)
                triggered.append(self._make_alert(
                    alert_type, current_price,
                    extra=f"Position has done {current_mult:.1f}x from entry"
                ))
                self.fired_alerts.add(alert_key)

        # Check loss alerts
        if current_pnl <= -25 and "LOSS_25" not in self.fired_alerts:
            triggered.append(self._make_alert(
                AlertType.LOSS_25, current_price,
                extra=f"Position is down {abs(current_pnl):.1f}%"
            ))
            self.fired_alerts.add("LOSS_25")

        if current_pnl <= -50 and "LOSS_50" not in self.fired_alerts:
            triggered.append(self._make_alert(
                AlertType.LOSS_50, current_price,
                extra=f"Position is down {abs(current_pnl):.1f}%"
            ))
            self.fired_alerts.add("LOSS_50")

        # Check custom price alerts
        for target_price in self.custom_price_alerts[:]:
            alert_key = f"CUSTOM_{target_price}"
            if alert_key not in self.fired_alerts:
                if self._price_crossed(target_price):
                    triggered.append(self._make_alert(
                        AlertType.CUSTOM, target_price,
                        extra=f"Price reached custom alert level ${target_price}"
                    ))
                    self.fired_alerts.add(alert_key)

        return triggered

    def _price_crossed(self, target: float) -> bool:
        """Check if current price has crossed a target level."""
        if self.direction == "LONG":
            return self.current_price >= target
        else:
            return self.current_price <= target

    def _price_hit_sl(self) -> bool:
        """Check if trailing stop loss has been hit (informational only)."""
        if self.direction == "LONG":
            return self.current_price <= self.stop_loss
        else:
            return self.current_price >= self.stop_loss

    def _price_hit_original_sl(self) -> bool:
        """Check if ORIGINAL stop loss has been hit (closes position)."""
        if self.direction == "LONG":
            return self.current_price <= self.original_stop_loss
        else:
            return self.current_price >= self.original_stop_loss

    def _price_hit_liquidation(self) -> bool:
        """Check if liquidation price has been hit."""
        if self.direction == "LONG":
            return self.current_price <= self.liquidation_price
        else:
            return self.current_price >= self.liquidation_price

    def _make_alert(self, alert_type: AlertType, trigger_price: float,
                    extra: str = "") -> dict:
        """Build an alert dict."""
        return {
            "type": alert_type,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "trigger_price": trigger_price,
            "pnl_pct": round(self.pnl_pct, 2),
            "pnl_usd": round(self.pnl_usd, 2),
            "leverage": self.leverage,
            "extra": extra,
            "timestamp": datetime.now(timezone.utc),
        }

    def to_dict(self) -> dict:
        """Serialize position for database storage."""
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "leverage": self.leverage,
            "size_usd": self.size_usd,
            "liquidation_price": self.liquidation_price,
            "pnl_pct": round(self.pnl_pct, 2),
            "pnl_usd": round(self.pnl_usd, 2),
            "is_open": self.is_open,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "close_reason": self.close_reason,
        }


class PositionMonitor:
    """
    Background service that monitors tracked positions and sends
    Telegram alerts when configured thresholds are hit.
    """

    def __init__(self, data_pipeline=None, telegram_bot=None):
        self._positions: dict[str, TrackedPosition] = {}
        self._data_pipeline = data_pipeline
        self._telegram = telegram_bot
        self._running = False
        self._check_interval = 5  # seconds

    @property
    def active_positions(self) -> list[TrackedPosition]:
        """Get all active (open) positions."""
        return [p for p in self._positions.values() if p.is_open]

    @property
    def position_count(self) -> int:
        return len(self.active_positions)

    def add_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float = 0.0,
        take_profit_1: float = 0.0,
        take_profit_2: float = 0.0,
        leverage: int = 1,
        size_usd: float = 0.0,
        profit_alerts: list[float] = None,
        multiplier_alerts: list[float] = None,
        custom_price_alerts: list[float] = None,
    ) -> TrackedPosition:
        """Add a new position to monitor."""
        position_id = len(self._positions) + 1
        position = TrackedPosition(
            position_id=position_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            leverage=leverage,
            size_usd=size_usd,
            profit_alert_pcts=profit_alerts,
            multiplier_alerts=multiplier_alerts,
            custom_price_alerts=custom_price_alerts,
        )
        self._positions[f"{symbol}_{position_id}"] = position
        log.info("position_added", symbol=symbol, direction=direction,
                entry=entry_price, sl=stop_loss, tp1=take_profit_1,
                leverage=leverage)
        return position

    async def remove_position(self, symbol: str, position_id: int = None) -> bool:
        """Remove a position from monitoring and close in database."""
        symbol_upper = symbol.upper()
        key = f"{symbol}_{position_id}" if position_id else None

        if key and key in self._positions:
            pos = self._positions[key]
            pos.is_open = False
            pos.closed_at = datetime.now(timezone.utc)
            pos.close_reason = "Manually closed"
            # Persist to database
            try:
                await close_position(pos.position_id, "Manually closed")
            except Exception as e:
                log.warning("db_close_failed", error=str(e))
            log.info("position_removed", symbol=symbol, id=position_id)
            return True

        # Find by symbol if no ID given (case-insensitive)
        for k, pos in self._positions.items():
            if pos.symbol.upper() == symbol_upper and pos.is_open:
                pos.is_open = False
                pos.closed_at = datetime.now(timezone.utc)
                pos.close_reason = "Manually closed"
                # Persist to database
                try:
                    await close_position(pos.position_id, "Manually closed")
                except Exception as e:
                    log.warning("db_close_failed", error=str(e))
                log.info("position_removed", symbol=symbol)
                return True

        return False

    def add_price_alert(self, symbol: str, target_price: float) -> bool:
        """Add a custom price alert to an existing position."""
        symbol_upper = symbol.upper()
        for pos in self._positions.values():
            if pos.symbol.upper() == symbol_upper and pos.is_open:
                pos.custom_price_alerts.append(target_price)
                log.info("price_alert_added", symbol=symbol, price=target_price)
                return True
        return False

    def set_alert_thresholds(self, symbol: str,
                              profit_pcts: list[float] = None,
                              multipliers: list[float] = None) -> bool:
        """Update alert thresholds for a position."""
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.is_open:
                if profit_pcts is not None:
                    pos.profit_alert_pcts = profit_pcts
                if multipliers is not None:
                    pos.multiplier_alerts = multipliers
                log.info("thresholds_updated", symbol=symbol)
                return True
        return False

    async def start_monitoring(self) -> None:
        """Start the background position monitoring loop."""
        self._running = True

        # Load any positions saved in database from previous runs
        await self._load_from_db()

        log.info("position_monitor_started", interval=f"{self._check_interval}s",
                loaded=len(self._positions))

        while self._running:
            try:
                await self._check_all_positions()
            except Exception as e:
                log.error("monitor_check_error", error=str(e))

            await asyncio.sleep(self._check_interval)

    async def stop_monitoring(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        log.info("position_monitor_stopped")

    async def _load_from_db(self) -> None:
        """Load open positions from database into memory.
        On restart, silently close any positions where SL was already hit.
        """
        try:
            positions = await get_active_positions()
            for p in positions:
                pos_id = p.get("id", 0)
                symbol = p.get("symbol", "")
                # Skip if already loaded
                if any(pos.symbol == symbol and pos.is_open
                       for pos in self._positions.values()):
                    continue

                direction = p.get("direction", "LONG").upper()
                entry_price = p.get("entry_price", 0)
                stop_loss = p.get("stop_loss", 0)

                # Check if this position's SL was already hit before we loaded
                # (prevents alert floods on redeploy)
                if stop_loss > 0 and entry_price > 0:
                    try:
                        # Quick price check
                        current = None
                        if self._data_pipeline:
                            token = await self._data_pipeline.build_token_profile(symbol)
                            if token and token.metrics.price > 0:
                                current = token.metrics.price

                        if current:
                            sl_hit = False
                            if direction == "LONG" and current <= stop_loss:
                                sl_hit = True
                            elif direction == "SHORT" and current >= stop_loss:
                                sl_hit = True

                            if sl_hit:
                                # Close silently in DB — don't send alert
                                from utils.database import close_position as db_close
                                await db_close(pos_id, "Stop Loss Hit (detected on restart)")
                                log.info("position_closed_on_load",
                                        symbol=symbol, reason="SL already hit")
                                continue  # Don't load this position
                    except Exception as check_err:
                        log.warning("sl_check_on_load_failed",
                                   symbol=symbol, error=str(check_err))

                tp1 = p.get("take_profit_1", 0)
                tp2 = p.get("take_profit_2", 0)
                leverage = p.get("leverage", 1)

                position = TrackedPosition(
                    position_id=pos_id,
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    leverage=leverage,
                    size_usd=p.get("size_usd", 0),
                    liquidation_price=p.get("liquidation_price", 0),
                )

                # Pre-populate fired_alerts based on current price
                # so we don't re-fire old alerts on restart
                try:
                    current = None
                    if self._data_pipeline:
                        token = await self._data_pipeline.build_token_profile(symbol)
                        if token and token.metrics.price > 0:
                            current = token.metrics.price

                    if current and current > 0:
                        position.current_price = current

                        # Check if TP levels already crossed
                        if tp1 > 0:
                            crossed = (current >= tp1) if direction == "LONG" else (current <= tp1)
                            if crossed:
                                position.fired_alerts.add("TP1")

                        if tp2 > 0:
                            crossed = (current >= tp2) if direction == "LONG" else (current <= tp2)
                            if crossed:
                                position.fired_alerts.add("TP2")

                        # Check if profit thresholds already crossed
                        pnl = position.pnl_pct
                        for pct in position.profit_alert_pcts:
                            if pnl >= pct:
                                position.fired_alerts.add(f"PROFIT_{pct}")

                        if position.fired_alerts:
                            log.info("alerts_pre_marked",
                                    symbol=symbol,
                                    fired=list(position.fired_alerts))
                except Exception as mark_err:
                    log.warning("alert_pre_mark_failed",
                               symbol=symbol, error=str(mark_err))

                self._positions[f"{symbol}_{pos_id}"] = position

            if positions:
                log.info("positions_loaded_from_db", count=len(positions))
        except Exception as e:
            log.warning("db_position_load_failed", error=str(e))

    async def _check_all_positions(self) -> None:
        """Check all active positions against current prices."""
        active = self.active_positions
        if not active:
            return

        for position in active:
            try:
                # Get current price from data pipeline
                current_price = await self._get_current_price(position.symbol)
                if current_price is None or current_price <= 0:
                    continue

                # === TRAILING STOP DISABLED ===\n                # Previously, the trailing stop moved position.stop_loss\n                # which caused false 'Stop Loss Hit' alerts on profitable trades.\n                # The original SL set by the signal is now the ONLY SL.\n                # Users can manually adjust SL via /set command if needed.", "StartLine": 491, "TargetContent": "                # === TRAILING STOP (matches backtester logic) ===\n                # Track best price and move SL to lock in profits\n                if position.stop_loss > 0 and position.entry_price > 0:\n                    if position.direction.upper() == \"LONG\":\n                        best = getattr(position, '_best_price', position.entry_price)\n                        if current_price > best:\n                            position._best_price = current_price\n                        best = position._best_price\n                        unrealized = (best - position.entry_price) / position.entry_price * 100\n                        if unrealized >= 5.0:  # Activate trailing at 5% profit\n                            trail_price = position.entry_price * (1 + unrealized * 0.3 / 100)\n                            if trail_price > position.stop_loss:\n                                old_sl = position.stop_loss\n                                position.stop_loss = trail_price\n                                log.info(\"trailing_stop_moved\", symbol=position.symbol,\n                                        direction=\"LONG\", old_sl=old_sl,\n                                        new_sl=trail_price, unrealized=f\"{unrealized:.1f}%\")\n                    elif position.direction.upper() == \"SHORT\":\n                        best = getattr(position, '_best_price', position.entry_price)\n                        if current_price < best:\n                            position._best_price = current_price\n                        best = position._best_price\n                        unrealized = (position.entry_price - best) / position.entry_price * 100\n                        if unrealized >= 5.0:  # Activate trailing at 5% profit\n                            trail_price = position.entry_price * (1 - unrealized * 0.3 / 100)\n                            if trail_price < position.stop_loss:\n                                old_sl = position.stop_loss\n                                position.stop_loss = trail_price\n                                log.info(\"trailing_stop_moved\", symbol=position.symbol,\n                                        direction=\"SHORT\", old_sl=old_sl,\n                                        new_sl=trail_price, unrealized=f\"{unrealized:.1f}%\")

                # Check all alert conditions
                triggered_alerts = position.check_alerts(current_price)

                # Send each triggered alert via Telegram
                for alert in triggered_alerts:
                    await self._send_alert(alert)

                # If position was closed by SL/Liquidation, persist to DB
                if not position.is_open and position.close_reason:
                    try:
                        from utils.database import close_position as db_close
                        await db_close(position.position_id, position.close_reason)
                        log.info("position_closed_in_db",
                                symbol=position.symbol,
                                reason=position.close_reason,
                                id=position.position_id)
                    except Exception as db_err:
                        log.warning("db_close_failed", error=str(db_err))

            except Exception as e:
                log.error("position_check_error",
                         symbol=position.symbol, error=str(e))

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """Get the current price for a symbol."""
        if self._data_pipeline is None:
            return None

        try:
            token = await self._data_pipeline.build_token_profile(symbol)
            if token and token.metrics.price > 0:
                return token.metrics.price
        except Exception as e:
            log.error("price_fetch_error", symbol=symbol, error=str(e))

        return None

    async def _send_alert(self, alert: dict) -> None:
        """Send a position alert via Telegram."""
        if self._telegram is None:
            log.warning("no_telegram_for_alert", alert_type=alert["type"].value)
            return

        message = format_position_alert(alert)
        try:
            await self._telegram.send_message(message)
            log.info("position_alert_sent",
                    type=alert["type"].value,
                    symbol=alert["symbol"])
        except Exception as e:
            log.error("alert_send_failed", error=str(e))

    def get_positions_summary(self) -> list[dict]:
        """Get summary of all tracked positions."""
        return [p.to_dict() for p in self._positions.values() if p.is_open]

    def get_all_positions_summary(self) -> list[dict]:
        """Get summary of all positions including closed."""
        return [p.to_dict() for p in self._positions.values()]


def format_position_alert(alert: dict) -> str:
    """Format a position alert for Telegram."""
    alert_type: AlertType = alert["type"]
    symbol = alert["symbol"]
    direction = alert["direction"]
    entry = alert["entry_price"]
    current = alert["current_price"]
    pnl_pct = alert["pnl_pct"]
    pnl_usd = alert["pnl_usd"]
    leverage = alert["leverage"]

    # Choose emoji based on alert type
    if alert_type in (AlertType.SL_HIT, AlertType.LIQUIDATED, AlertType.LOSS_25, AlertType.LOSS_50):
        emoji = "🔴"
    elif alert_type in (AlertType.TP1_HIT, AlertType.TP2_HIT):
        emoji = "🎯"
    elif alert_type in (AlertType.PROFIT_X5, AlertType.PROFIT_X10):
        emoji = "🚀"
    else:
        emoji = "💰"

    lines = [
        f"{emoji} {alert_type.value}: ${symbol}",
        "",
        f"Direction: {direction}",
        f"Entry: ${entry:.6f}" if entry < 1 else f"Entry: ${entry:,.2f}",
        f"Current: ${current:.6f}" if current < 1 else f"Current: ${current:,.2f}",
        f"Leverage: {leverage}x",
        "",
        f"PnL: {pnl_pct:+.1f}%",
    ]

    if pnl_usd != 0:
        lines.append(f"PnL USD: ${pnl_usd:+,.2f}")

    if alert.get("extra"):
        lines.append("")
        lines.append(alert["extra"])

    # Add action suggestion based on type
    if alert_type == AlertType.SL_HIT:
        lines.extend(["", "Position has been stopped out. Review your analysis."])
    elif alert_type == AlertType.LIQUIDATED:
        lines.extend(["", "⚠️ Position has been liquidated. All margin lost."])
    elif alert_type == AlertType.TP1_HIT:
        lines.extend(["", "Consider taking partial profits and moving SL to breakeven."])
    elif alert_type == AlertType.TP2_HIT:
        lines.extend(["", "Final take profit hit. Consider closing the remaining position."])

    return "\n".join(lines)
