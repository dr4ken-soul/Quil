"""
Execution Engine — Phase 11: Live Trading

Places real trades through CCXT on Bybit (primary) and other exchanges.
Handles leverage setting, market orders, conditional SL/TP placement,
position sync, and auto-tracking.

Paper trading mode is ON by default. Switch to live with TRADING_MODE=live.
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
from utils.database import save_trade_signal, save_position
from config.settings import EXCHANGE_CONFIGS

log = get_logger("execution_engine")


class ExecutionEngine:
    """
    Executes trades through CCXT.

    Supports:
    - Market orders with automatic leverage setting
    - Conditional stop loss and take profit orders
    - Position tracking after execution
    - Paper trading mode (default)
    - Bybit as primary exchange
    """

    def __init__(self, risk_manager: RiskManager = None,
                 paper_trading: bool = True):
        self.risk_manager = risk_manager or RiskManager()
        self.paper_trading = paper_trading
        self._exchanges: dict[str, ccxt.Exchange] = {}
        self._active_orders: dict[str, dict] = {}
        # Stores pending trade signals waiting for user confirmation
        self._pending_trades: dict[str, dict] = {}

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
                log.info("exchange_connected", exchange=name,
                        markets=len(exchange.markets))

            except Exception as e:
                log.error("exchange_connection_failed",
                         exchange=name, error=str(e))
                results[name] = False

        return results

    def store_pending_trade(self, token_symbol: str, signal: TradeSignal,
                            token: Token) -> str:
        """
        Store a trade signal for confirmation. Returns a confirmation ID.
        """
        import hashlib
        confirm_id = hashlib.md5(
            f"{token_symbol}{datetime.now().timestamp()}".encode()
        ).hexdigest()[:8]

        self._pending_trades[confirm_id] = {
            "signal": signal,
            "token": token,
            "created": datetime.now(timezone.utc),
        }

        # Clean old pending trades (older than 5 minutes)
        cutoff = datetime.now(timezone.utc)
        expired = [
            k for k, v in self._pending_trades.items()
            if (cutoff - v["created"]).seconds > 300
        ]
        for k in expired:
            del self._pending_trades[k]

        return confirm_id

    def get_pending_trade(self, confirm_id: str) -> Optional[dict]:
        """Get a pending trade by confirmation ID."""
        return self._pending_trades.pop(confirm_id, None)

    async def execute_trade(self, signal: TradeSignal, token: Token,
                             exchange_name: str = None,
                             position_size_usd: float = None) -> dict:
        """
        Execute a trade based on the signal.

        In paper trading mode: logs the trade but places no real order.
        In live mode: places market order + SL/TP on the exchange.

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

        # Save signal to database
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
            signal_id = None

        # Paper trading mode
        if self.paper_trading:
            log.info("paper_trade_executed",
                    token=adjusted_signal.token_symbol,
                    direction=adjusted_signal.direction.value,
                    entry=adjusted_signal.risk.entry_price,
                    sl=adjusted_signal.risk.stop_loss,
                    tp1=adjusted_signal.risk.take_profit_1)

            self.risk_manager.record_trade_open(adjusted_signal, token)

            # Auto-track in position monitor
            position_id = await self._auto_track_position(adjusted_signal, "paper")

            return {
                "executed": True,
                "mode": "paper",
                "order_id": f"PAPER-{signal_id or 'unknown'}",
                "signal": adjusted_signal,
                "position_id": position_id,
                "warnings": risk_check.get("warnings", []),
                "message": "📝 Paper trade logged. No real order was placed."
            }

        # === LIVE TRADING MODE ===

        # Select exchange
        if not exchange_name:
            exchange_name = self._select_exchange(token)

        if exchange_name not in self._exchanges:
            return {
                "executed": False,
                "mode": "live",
                "reason": f"Exchange '{exchange_name}' not connected. "
                          f"Add your API keys to Railway variables.",
                "signal": adjusted_signal,
            }

        exchange = self._exchanges[exchange_name]

        try:
            # Calculate trade amount
            if position_size_usd:
                trade_value = position_size_usd
            else:
                trade_value = (
                    self.risk_manager.portfolio_value *
                    adjusted_signal.risk.position_size_pct / 100
                )

            if trade_value <= 0:
                trade_value = 10.0  # Minimum $10 trade

            entry_price = adjusted_signal.risk.entry_price
            if entry_price <= 0:
                return {
                    "executed": False,
                    "mode": "live",
                    "reason": "Invalid entry price",
                    "signal": adjusted_signal,
                }

            symbol = f"{token.symbol}/USDT"
            leverage = adjusted_signal.risk.leverage or 3

            # Step 1: Set leverage
            await self._set_leverage(exchange, exchange_name, symbol, leverage)

            # Step 2: Place market order
            side = "buy" if adjusted_signal.direction == Direction.LONG else "sell"
            amount = trade_value / entry_price

            log.info("placing_live_order",
                    exchange=exchange_name,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    leverage=leverage)

            order = await exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=amount,
                params=self._get_order_params(exchange_name, "market"),
            )

            order_id = order.get("id", "unknown")
            fill_price = order.get("average") or order.get("price") or entry_price
            filled_amount = order.get("filled") or amount

            log.info("order_filled",
                    order_id=order_id,
                    price=fill_price,
                    amount=filled_amount)

            # Step 3: Place stop loss
            sl_order_id = await self._place_stop_loss(
                exchange, exchange_name, symbol,
                adjusted_signal, filled_amount
            )

            # Step 4: Place take profit (50% at TP1)
            tp_order_id = await self._place_take_profit(
                exchange, exchange_name, symbol,
                adjusted_signal, filled_amount
            )

            # Step 5: Track the position
            adjusted_signal.executed = True
            adjusted_signal.execution_price = fill_price
            adjusted_signal.execution_time = datetime.now(timezone.utc)

            self.risk_manager.record_trade_open(adjusted_signal, token)

            # Auto-track in position monitor
            position_id = await self._auto_track_position(
                adjusted_signal, exchange_name,
                fill_price=fill_price
            )

            # Store order IDs
            self._active_orders[token.symbol] = {
                "main_order": order_id,
                "sl_order": sl_order_id,
                "tp_order": tp_order_id,
                "exchange": exchange_name,
                "symbol": symbol,
                "direction": adjusted_signal.direction.value,
                "amount": filled_amount,
                "entry_price": fill_price,
                "leverage": leverage,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            return {
                "executed": True,
                "mode": "live",
                "exchange": exchange_name,
                "order_id": order_id,
                "price": fill_price,
                "amount": filled_amount,
                "leverage": leverage,
                "sl_order": sl_order_id,
                "tp_order": tp_order_id,
                "position_id": position_id,
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

    async def close_position(self, token_symbol: str,
                              exchange_name: str = None) -> dict:
        """
        Close a position on the exchange. Cancels SL/TP orders and
        places a market close order.
        """
        if self.paper_trading:
            return {
                "closed": True,
                "mode": "paper",
                "message": f"Paper position for ${token_symbol} closed.",
            }

        # Get stored order info
        order_info = self._active_orders.get(token_symbol)
        if not order_info:
            return {
                "closed": False,
                "reason": f"No active position found for ${token_symbol}. "
                          f"Use the exchange directly to manage positions.",
            }

        exchange_name = order_info.get("exchange", exchange_name)
        if exchange_name not in self._exchanges:
            return {
                "closed": False,
                "reason": f"Exchange '{exchange_name}' not connected.",
            }

        exchange = self._exchanges[exchange_name]
        symbol = order_info["symbol"]
        amount = order_info["amount"]
        direction = order_info["direction"]

        try:
            # Cancel SL/TP orders first
            for order_type in ["sl_order", "tp_order"]:
                oid = order_info.get(order_type)
                if oid:
                    try:
                        await exchange.cancel_order(oid, symbol)
                        log.info("order_cancelled", order_id=oid, type=order_type)
                    except Exception:
                        pass  # Order may already be filled/cancelled

            # Place close order (opposite direction)
            close_side = "sell" if direction == "long" else "buy"

            close_order = await exchange.create_order(
                symbol=symbol,
                type="market",
                side=close_side,
                amount=amount,
                params=self._get_order_params(exchange_name, "close"),
            )

            close_price = close_order.get("average") or close_order.get("price", 0)
            entry_price = order_info["entry_price"]

            # Calculate PnL
            if direction == "long":
                pnl_pct = (close_price - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - close_price) / entry_price * 100

            leverage = order_info.get("leverage", 1)
            pnl_pct_leveraged = pnl_pct * leverage

            # Clean up
            del self._active_orders[token_symbol]

            return {
                "closed": True,
                "mode": "live",
                "exchange": exchange_name,
                "close_price": close_price,
                "entry_price": entry_price,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_pct_leveraged": round(pnl_pct_leveraged, 2),
                "order_id": close_order.get("id"),
            }

        except Exception as e:
            log.error("position_close_failed",
                     token=token_symbol, error=str(e))
            return {
                "closed": False,
                "reason": str(e),
            }

    async def get_exchange_balance(self, exchange_name: str = None) -> dict:
        """Get USDT balance from the exchange."""
        if not exchange_name:
            exchange_name = self._get_primary_exchange()

        if exchange_name not in self._exchanges:
            return {"balance": 0, "error": "Exchange not connected"}

        try:
            balance = await self._exchanges[exchange_name].fetch_balance()
            usdt = balance.get("USDT", {})
            return {
                "exchange": exchange_name,
                "total": usdt.get("total", 0),
                "free": usdt.get("free", 0),
                "used": usdt.get("used", 0),
            }
        except Exception as e:
            return {"balance": 0, "error": str(e)}

    async def get_open_positions(self, exchange_name: str = None) -> list:
        """Fetch open positions from the exchange."""
        if not exchange_name:
            exchange_name = self._get_primary_exchange()

        if exchange_name not in self._exchanges:
            return []

        try:
            positions = await self._exchanges[exchange_name].fetch_positions()
            # Filter to non-zero positions
            return [
                p for p in positions
                if float(p.get("contracts", 0) or 0) > 0
            ]
        except Exception as e:
            log.error("fetch_positions_failed", error=str(e))
            return []

    # ── Private helpers ──────────────────────────────────────────────────────

    async def _set_leverage(self, exchange, exchange_name: str,
                             symbol: str, leverage: int) -> None:
        """Set leverage for the trading pair."""
        try:
            if exchange_name == "bybit":
                await exchange.set_leverage(leverage, symbol, params={
                    "buyLeverage": str(leverage),
                    "sellLeverage": str(leverage),
                })
            else:
                await exchange.set_leverage(leverage, symbol)
            log.info("leverage_set", exchange=exchange_name,
                    symbol=symbol, leverage=leverage)
        except Exception as e:
            log.warning("set_leverage_failed", error=str(e),
                       note="Some exchanges set leverage via order params")

    async def _place_stop_loss(self, exchange, exchange_name: str,
                                symbol: str, signal: TradeSignal,
                                amount: float) -> Optional[str]:
        """Place a stop loss order."""
        sl_price = signal.risk.stop_loss
        if not sl_price or sl_price <= 0:
            return None

        sl_side = "sell" if signal.direction == Direction.LONG else "buy"

        try:
            if exchange_name == "bybit":
                # Bybit uses conditional orders for SL
                order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=sl_side,
                    amount=amount,
                    params={
                        "triggerPrice": str(sl_price),
                        "triggerDirection": 2 if signal.direction == Direction.LONG else 1,
                        "orderType": "Market",
                        "reduceOnly": True,
                    },
                )
            else:
                order = await exchange.create_order(
                    symbol=symbol,
                    type="stop_market" if exchange.has.get("createStopMarketOrder") else "stop",
                    side=sl_side,
                    amount=amount,
                    price=sl_price,
                    params={
                        "stopPrice": sl_price,
                        "reduceOnly": True,
                    },
                )

            sl_id = order.get("id")
            log.info("stop_loss_placed", order_id=sl_id, price=sl_price)
            return sl_id

        except Exception as e:
            log.warning("stop_loss_placement_failed",
                       exchange=exchange_name, error=str(e))
            return None

    async def _place_take_profit(self, exchange, exchange_name: str,
                                  symbol: str, signal: TradeSignal,
                                  amount: float) -> Optional[str]:
        """Place a take profit order (50% at TP1)."""
        tp_price = signal.risk.take_profit_1
        if not tp_price or tp_price <= 0:
            return None

        tp_side = "sell" if signal.direction == Direction.LONG else "buy"
        tp_amount = amount * 0.5  # Close 50% at TP1

        try:
            if exchange_name == "bybit":
                order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=tp_side,
                    amount=tp_amount,
                    params={
                        "triggerPrice": str(tp_price),
                        "triggerDirection": 1 if signal.direction == Direction.LONG else 2,
                        "orderType": "Market",
                        "reduceOnly": True,
                    },
                )
            else:
                order = await exchange.create_order(
                    symbol=symbol,
                    type="take_profit_market" if exchange.has.get("createTakeProfitMarketOrder") else "limit",
                    side=tp_side,
                    amount=tp_amount,
                    price=tp_price,
                    params={
                        "stopPrice": tp_price,
                        "reduceOnly": True,
                    },
                )

            tp_id = order.get("id")
            log.info("take_profit_placed", order_id=tp_id, price=tp_price)
            return tp_id

        except Exception as e:
            log.warning("take_profit_placement_failed",
                       exchange=exchange_name, error=str(e))
            return None

    async def _auto_track_position(self, signal: TradeSignal,
                                     exchange_name: str,
                                     fill_price: float = None) -> Optional[int]:
        """Auto-save the position to database for monitoring."""
        try:
            entry = fill_price or signal.risk.entry_price
            lev = signal.risk.leverage if hasattr(signal.risk, 'leverage') else 3

            # Calculate liquidation price
            if signal.direction == Direction.LONG:
                liq_price = entry * (1 - 1.0 / lev) if lev > 0 else 0
            else:
                liq_price = entry * (1 + 1.0 / lev) if lev > 0 else 0

            position_data = {
                "symbol": signal.token_symbol,
                "direction": signal.direction.value,
                "entry_price": entry,
                "stop_loss": signal.risk.stop_loss,
                "take_profit_1": signal.risk.take_profit_1,
                "take_profit_2": signal.risk.take_profit_2,
                "leverage": lev,
                "size_usd": signal.risk.position_size_pct * self.risk_manager.portfolio_value / 100,
                "liquidation_price": liq_price,
                "profit_alerts": "[]",
                "multiplier_alerts": "[]",
                "custom_price_alerts": "[]",
                "exchange": exchange_name,
            }

            position_id = await save_position(position_data)
            log.info("position_auto_tracked",
                    symbol=signal.token_symbol, position_id=position_id)
            return position_id

        except Exception as e:
            log.warning("auto_track_failed", error=str(e))
            return None

    def _select_exchange(self, token: Token) -> str:
        """Select the best exchange for execution."""
        # Prefer Bybit since the user trades there
        if "bybit" in self._exchanges:
            return "bybit"

        # Then try others
        for name in ["binance", "okx", "bitget", "gate"]:
            if name in self._exchanges:
                return name

        return "bybit"  # Default

    def _get_primary_exchange(self) -> str:
        """Get the first connected exchange."""
        if "bybit" in self._exchanges:
            return "bybit"
        if self._exchanges:
            return list(self._exchanges.keys())[0]
        return "bybit"

    def _get_order_params(self, exchange_name: str, order_type: str) -> dict:
        """Get exchange-specific order params."""
        if exchange_name == "bybit":
            if order_type == "close":
                return {"reduceOnly": True}
            return {}
        return {}

    def get_execution_status(self) -> dict:
        """Get current execution engine status."""
        return {
            "paper_trading": self.paper_trading,
            "connected_exchanges": list(self._exchanges.keys()),
            "active_orders": len(self._active_orders),
            "pending_confirmations": len(self._pending_trades),
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
