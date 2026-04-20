"""
Execution Engine

Places trades through CCXT based on aggregated signals with risk
parameters applied. Handles order placement, monitoring, and
position management across all configured exchanges.

All trade decisions are logged with full reasoning to the database.
"""

import asyncio
import ccxt.async_support as ccxt
from typing import Optional
from datetime import datetime, timezone

from models.token import Token
from models.trade import TradeSignal
from models import Direction
from core.risk_manager import RiskManager
from utils.logger import get_logger
from utils.database import save_trade_signal
from config.settings import EXCHANGE_CONFIGS

log = get_logger("execution_engine")


class ExecutionEngine:
    """
    Executes trades through CCXT based on validated signals.

    Supports:
    - Market and limit orders
    - Stop loss and take profit placement
    - Multi-exchange execution
    - Paper trading mode (default)
    - Full trade logging with reasoning

    Paper trading mode is ON by default. The bot will log trades
    and send Telegram notifications but will not place real orders
    until paper_trading is set to False.
    """

    def __init__(self, risk_manager: RiskManager = None,
                 paper_trading: bool = True):
        self.risk_manager = risk_manager or RiskManager()
        self.paper_trading = paper_trading
        self._exchanges: dict[str, ccxt.Exchange] = {}
        self._active_orders: dict[str, dict] = {}

    async def initialize_exchanges(self) -> dict[str, bool]:
        """
        Initialize CCXT exchange connections for all configured exchanges.
        Returns a dict of exchange name -> connection success.
        """
        results = {}

        for name, config in EXCHANGE_CONFIGS.items():
            if not config.get("apiKey"):
                results[name] = False
                continue

            try:
                exchange_class = getattr(ccxt, name, None)
                if exchange_class is None:
                    log.warning("exchange_not_found", exchange=name)
                    results[name] = False
                    continue

                exchange = exchange_class({
                    "apiKey": config["apiKey"],
                    "secret": config["secret"],
                    "password": config.get("password"),
                    "options": config.get("options", {}),
                    "enableRateLimit": True,
                })

                # Test connection
                await exchange.load_markets()
                self._exchanges[name] = exchange
                results[name] = True
                log.info("exchange_connected", exchange=name)

            except Exception as e:
                log.error("exchange_connection_failed",
                         exchange=name, error=str(e))
                results[name] = False

        return results

    async def execute_trade(self, signal: TradeSignal, token: Token,
                             exchange_name: str = None) -> dict:
        """
        Execute a trade based on the signal.

        In paper trading mode: logs the trade, sends notification, but does not
        place any real order.

        In live mode: places the order through CCXT on the specified exchange.

        Returns execution result dict.
        """
        # Run risk validation
        risk_check = self.risk_manager.validate_trade(signal, token)

        if not risk_check["approved"]:
            return {
                "executed": False,
                "mode": "paper" if self.paper_trading else "live",
                "reason": "Risk check failed",
                "rejections": risk_check["rejections"],
                "signal": signal,
            }

        adjusted_signal = risk_check["adjusted_signal"]

        # Save signal to database regardless of mode
        signal_data = {
            "token_symbol": adjusted_signal.token_symbol,
            "direction": adjusted_signal.direction.value,
            "confidence": adjusted_signal.confidence,
            "timeframe": adjusted_signal.timeframe,
            "entry_price": adjusted_signal.risk.entry_price,
            "stop_loss": adjusted_signal.risk.stop_loss,
            "take_profit_1": adjusted_signal.risk.take_profit_1,
            "take_profit_2": adjusted_signal.risk.take_profit_2,
            "risk_reward": adjusted_signal.risk.risk_reward,
            "technical_signals": [
                {"name": s.name, "observation": s.observation}
                for s in adjusted_signal.technical_signals
            ],
            "onchain_signals": adjusted_signal.onchain_signals,
            "trap_detection": {
                "regime": adjusted_signal.trap_detection.regime.value,
                "trap_fired": adjusted_signal.trap_detection.trap_fired.value,
                "confidence": adjusted_signal.trap_detection.confidence,
                "gate_trace": adjusted_signal.trap_detection.gate_trace,
            },
            "crime_pump_status": adjusted_signal.crime_pump_status.value,
            "reasoning": adjusted_signal.reasoning_summary,
            "framework_source": ", ".join(adjusted_signal.contributing_frameworks),
        }

        try:
            signal_id = await save_trade_signal(signal_data)
            adjusted_signal.signal_id = signal_id
        except Exception as e:
            log.error("signal_save_error", error=str(e))

        # Paper trading mode
        if self.paper_trading:
            log.info("paper_trade_executed",
                    token=adjusted_signal.token_symbol,
                    direction=adjusted_signal.direction.value,
                    entry=adjusted_signal.risk.entry_price,
                    sl=adjusted_signal.risk.stop_loss,
                    tp1=adjusted_signal.risk.take_profit_1)

            self.risk_manager.record_trade_open(adjusted_signal, token)

            return {
                "executed": True,
                "mode": "paper",
                "order_id": f"PAPER-{signal_id or 'unknown'}",
                "signal": adjusted_signal,
                "warnings": risk_check.get("warnings", []),
                "message": "Paper trade logged. No real order was placed."
            }

        # Live trading mode
        if not exchange_name:
            exchange_name = self._select_exchange(token)

        if exchange_name not in self._exchanges:
            return {
                "executed": False,
                "mode": "live",
                "reason": f"Exchange {exchange_name} not connected",
                "signal": adjusted_signal,
            }

        try:
            result = await self._place_order(
                exchange_name, adjusted_signal, token
            )

            adjusted_signal.executed = True
            adjusted_signal.execution_price = result.get("price", adjusted_signal.risk.entry_price)
            adjusted_signal.execution_time = datetime.now(timezone.utc)

            self.risk_manager.record_trade_open(adjusted_signal, token)

            log.info("live_trade_executed",
                    token=adjusted_signal.token_symbol,
                    exchange=exchange_name,
                    order_id=result.get("id"))

            return {
                "executed": True,
                "mode": "live",
                "exchange": exchange_name,
                "order_id": result.get("id"),
                "price": result.get("price"),
                "amount": result.get("amount"),
                "signal": adjusted_signal,
                "warnings": risk_check.get("warnings", []),
            }

        except Exception as e:
            log.error("trade_execution_failed",
                     token=adjusted_signal.token_symbol,
                     exchange=exchange_name,
                     error=str(e))
            return {
                "executed": False,
                "mode": "live",
                "reason": str(e),
                "signal": adjusted_signal,
            }

    async def _place_order(self, exchange_name: str, signal: TradeSignal,
                            token: Token) -> dict:
        """Place the actual order through CCXT."""
        exchange = self._exchanges[exchange_name]
        symbol = f"{token.symbol}/USDT"

        # Calculate order amount
        trade_value = (
            self.risk_manager.portfolio_value *
            signal.risk.position_size_pct / 100
        )
        amount = trade_value / signal.risk.entry_price if signal.risk.entry_price > 0 else 0

        side = "buy" if signal.direction == Direction.LONG else "sell"

        # Place market order
        order = await exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
        )

        # Place stop loss
        sl_side = "sell" if signal.direction == Direction.LONG else "buy"
        try:
            await exchange.create_order(
                symbol=symbol,
                type="stop_loss",
                side=sl_side,
                amount=amount,
                price=signal.risk.stop_loss,
                params={"stopPrice": signal.risk.stop_loss},
            )
        except Exception as e:
            log.warning("stop_loss_placement_failed", error=str(e))

        # Place take profit
        tp_side = sl_side
        try:
            await exchange.create_order(
                symbol=symbol,
                type="limit",
                side=tp_side,
                amount=amount * 0.5,  # 50% at TP1
                price=signal.risk.take_profit_1,
            )
        except Exception as e:
            log.warning("take_profit_placement_failed", error=str(e))

        return order

    def _select_exchange(self, token: Token) -> str:
        """Select the best exchange for execution based on token presence."""
        # Prefer exchanges where the token is listed
        listed = token.exchange_presence.exchanges_listed
        for ex in listed:
            if ex.lower() in self._exchanges:
                return ex.lower()

        # Fall back to first available exchange
        if self._exchanges:
            return list(self._exchanges.keys())[0]

        return "binance"  # Default

    def get_execution_status(self) -> dict:
        """Get current execution engine status."""
        return {
            "paper_trading": self.paper_trading,
            "connected_exchanges": list(self._exchanges.keys()),
            "active_orders": len(self._active_orders),
            "risk_summary": self.risk_manager.get_risk_summary(),
        }

    async def close(self) -> None:
        """Close all exchange connections."""
        for name, exchange in self._exchanges.items():
            try:
                await exchange.close()
            except Exception:
                pass
        self._exchanges.clear()
