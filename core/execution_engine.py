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
                 paper_trading: bool = True,
                 position_monitor=None):
        self.risk_manager = risk_manager or RiskManager()
        self.paper_trading = paper_trading
        self.position_monitor = position_monitor
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
        """Get a pending trade by confirmation ID (does NOT remove it)."""
        return self._pending_trades.get(confirm_id, None)

    def consume_pending_trade(self, confirm_id: str) -> Optional[dict]:
        """Get and REMOVE a pending trade (used at final execution)."""
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
            position_id = await self._auto_track_position(
                adjusted_signal, "paper",
                position_size_usd=position_size_usd
            )

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

    async def modify_stop_loss(self, ticker: str, new_sl: float) -> dict:
        """
        Modify the SL on a live exchange position.
        Cancels the old SL conditional order and places a new one.
        """
        order_info = self._active_orders.get(ticker)
        if not order_info:
            return {"success": False, "reason": f"No active order found for {ticker}"}

        exchange_name = order_info["exchange"]
        exchange = self._exchanges.get(exchange_name)
        if not exchange:
            return {"success": False, "reason": f"Exchange {exchange_name} not connected"}

        symbol = order_info["symbol"]
        old_sl_id = order_info.get("sl_order")
        direction = order_info["direction"]
        amount = order_info["amount"]

        try:
            # Cancel old SL order
            if old_sl_id:
                try:
                    await exchange.cancel_order(old_sl_id, symbol)
                    log.info("old_sl_cancelled", order_id=old_sl_id)
                except Exception as cancel_err:
                    log.warning("sl_cancel_failed", error=str(cancel_err))

            # Place new SL
            sl_side = "sell" if direction == "LONG" else "buy"
            trigger_dir = 2 if direction == "LONG" else 1

            if exchange_name == "bybit":
                order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=sl_side,
                    amount=amount,
                    params={
                        "triggerPrice": str(new_sl),
                        "triggerDirection": trigger_dir,
                        "orderType": "Market",
                        "reduceOnly": True,
                    },
                )
            else:
                order = await exchange.create_order(
                    symbol=symbol,
                    type="stop_market",
                    side=sl_side,
                    amount=amount,
                    params={
                        "stopPrice": new_sl,
                        "reduceOnly": True,
                    },
                )

            new_sl_id = order.get("id")
            self._active_orders[ticker]["sl_order"] = new_sl_id
            log.info("sl_modified", symbol=symbol, new_sl=new_sl, order_id=new_sl_id)

            return {"success": True, "order_id": new_sl_id, "new_sl": new_sl}

        except Exception as e:
            log.error("sl_modify_failed", symbol=symbol, error=str(e))
            return {"success": False, "reason": str(e)}

    async def modify_take_profit(self, ticker: str, new_tp: float) -> dict:
        """
        Modify the TP on a live exchange position.
        Cancels the old TP conditional order and places a new one.
        """
        order_info = self._active_orders.get(ticker)
        if not order_info:
            return {"success": False, "reason": f"No active order found for {ticker}"}

        exchange_name = order_info["exchange"]
        exchange = self._exchanges.get(exchange_name)
        if not exchange:
            return {"success": False, "reason": f"Exchange {exchange_name} not connected"}

        symbol = order_info["symbol"]
        old_tp_id = order_info.get("tp_order")
        direction = order_info["direction"]
        amount = order_info["amount"]

        try:
            # Cancel old TP order
            if old_tp_id:
                try:
                    await exchange.cancel_order(old_tp_id, symbol)
                    log.info("old_tp_cancelled", order_id=old_tp_id)
                except Exception as cancel_err:
                    log.warning("tp_cancel_failed", error=str(cancel_err))

            # Place new TP
            tp_side = "sell" if direction == "LONG" else "buy"
            trigger_dir = 1 if direction == "LONG" else 2

            if exchange_name == "bybit":
                order = await exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=tp_side,
                    amount=amount,
                    params={
                        "triggerPrice": str(new_tp),
                        "triggerDirection": trigger_dir,
                        "orderType": "Market",
                        "reduceOnly": True,
                    },
                )
            else:
                order = await exchange.create_order(
                    symbol=symbol,
                    type="take_profit_market",
                    side=tp_side,
                    amount=amount,
                    params={
                        "stopPrice": new_tp,
                        "reduceOnly": True,
                    },
                )

            new_tp_id = order.get("id")
            self._active_orders[ticker]["tp_order"] = new_tp_id
            log.info("tp_modified", symbol=symbol, new_tp=new_tp, order_id=new_tp_id)

            return {"success": True, "order_id": new_tp_id, "new_tp": new_tp}

        except Exception as e:
            log.error("tp_modify_failed", symbol=symbol, error=str(e))
            return {"success": False, "reason": str(e)}

    async def close_partial(self, ticker: str, close_pct: float = 50.0) -> dict:
        """
        Close a percentage of a live position (take partial profits).
        close_pct: percentage to close (e.g. 50 = close 50%)
        """
        order_info = self._active_orders.get(ticker)
        if not order_info:
            return {"success": False, "reason": f"No active order found for {ticker}"}

        exchange_name = order_info["exchange"]
        exchange = self._exchanges.get(exchange_name)
        if not exchange:
            return {"success": False, "reason": f"Exchange {exchange_name} not connected"}

        symbol = order_info["symbol"]
        direction = order_info["direction"]
        total_amount = order_info["amount"]
        close_amount = total_amount * (close_pct / 100.0)

        try:
            # Place market order to close partial
            close_side = "sell" if direction == "LONG" else "buy"

            order = await exchange.create_order(
                symbol=symbol,
                type="market",
                side=close_side,
                amount=close_amount,
                params={"reduceOnly": True},
            )

            order_id = order.get("id")
            fill_price = order.get("average") or order.get("price", 0)

            # Update remaining amount
            self._active_orders[ticker]["amount"] = total_amount - close_amount

            log.info("partial_close",
                    symbol=symbol,
                    closed_pct=close_pct,
                    closed_amount=close_amount,
                    remaining=total_amount - close_amount,
                    fill_price=fill_price)

            return {
                "success": True,
                "order_id": order_id,
                "closed_pct": close_pct,
                "closed_amount": close_amount,
                "remaining_amount": total_amount - close_amount,
                "fill_price": fill_price,
            }

        except Exception as e:
            log.error("partial_close_failed", symbol=symbol, error=str(e))
            return {"success": False, "reason": str(e)}

    async def _auto_track_position(self, signal: TradeSignal,
                                     exchange_name: str,
                                     fill_price: float = None,
                                     position_size_usd: float = None) -> Optional[int]:
        """Auto-save the position to database AND add to live monitor."""
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
                "size_usd": position_size_usd or (signal.risk.position_size_pct * self.risk_manager.portfolio_value / 100),
                "liquidation_price": liq_price,
                "profit_alerts": "[]",
                "multiplier_alerts": "[]",
                "custom_price_alerts": "[]",
            }

            # Save to database
            position_id = await save_position(position_data)

            # Also add to in-memory position monitor for /positions
            if self.position_monitor is not None:
                self.position_monitor.add_position(
                    symbol=signal.token_symbol,
                    direction=signal.direction.value.upper(),
                    entry_price=entry,
                    stop_loss=signal.risk.stop_loss,
                    take_profit_1=signal.risk.take_profit_1,
                    take_profit_2=signal.risk.take_profit_2,
                    leverage=lev,
                    size_usd=position_data["size_usd"],
                )

            log.info("position_auto_tracked",
                    symbol=signal.token_symbol, position_id=position_id)
            return position_id

        except Exception as e:
            log.warning("auto_track_failed", error=str(e))
            return None

    async def _monitor_paper_limit_order(self, signal: TradeSignal, token: Token,
                                          limit_price: float, signal_id,
                                          timeout_minutes: int = 15) -> None:
        """
        Background monitor for paper limit orders.
        Checks the real market price every 30s. When the price hits the limit,
        fills the order and starts tracking the position.
        """
        from core.data_pipeline import DataPipeline

        check_interval = 30
        max_checks = (timeout_minutes * 60) // check_interval
        token_symbol = signal.token_symbol
        key = f"PAPER-LIMIT-{token_symbol}"

        for i in range(max_checks):
            await asyncio.sleep(check_interval)

            # Check if order was cancelled
            order_info = self._active_orders.get(key)
            if not order_info or order_info.get("status") != "pending":
                return

            try:
                # Get fresh price
                if self.position_monitor and self.position_monitor._data_pipeline:
                    fresh_token = await self.position_monitor._data_pipeline.build_token_profile(token_symbol)
                    if fresh_token and fresh_token.metrics.price > 0:
                        current_price = fresh_token.metrics.price
                    else:
                        continue
                else:
                    continue

                # Check if limit price has been hit
                filled = False
                if signal.direction == Direction.LONG:
                    # Long limit buy: fills when price drops TO or BELOW limit
                    if current_price <= limit_price:
                        filled = True
                else:
                    # Short limit sell: fills when price rises TO or ABOVE limit
                    if current_price >= limit_price:
                        filled = True

                if filled:
                    log.info("paper_limit_filled",
                            token=token_symbol, limit=limit_price,
                            market=current_price)

                    # Track the position now
                    self.risk_manager.record_trade_open(signal, token)
                    position_id = await self._auto_track_position(
                        signal, "paper", fill_price=limit_price
                    )

                    # Update status
                    self._active_orders[key]["status"] = "filled"

                    # Send Telegram notification if available
                    if self.position_monitor and self.position_monitor._telegram:
                        price_fmt = lambda p: f"${p:,.6f}" if p < 1 else f"${p:,.2f}"
                        fill_msg = (
                            f"✅ Paper LIMIT Order FILLED!\n\n"
                            f"Token: ${token_symbol}\n"
                            f"Direction: {signal.direction.value.upper()}\n"
                            f"Fill Price: {price_fmt(limit_price)}\n"
                            f"Market Price: {price_fmt(current_price)}\n"
                            f"SL: {price_fmt(signal.risk.stop_loss)}\n"
                            f"TP: {price_fmt(signal.risk.take_profit_1)}\n\n"
                            f"Position is now being tracked. Use /positions to monitor."
                        )
                        try:
                            await self.position_monitor._telegram.send_message(fill_msg)
                        except Exception:
                            pass

                    return

            except Exception as e:
                log.warning("paper_limit_check_error",
                           token=token_symbol, error=str(e))

        # TIMEOUT — order not filled
        log.info("paper_limit_timeout", token=token_symbol, minutes=timeout_minutes)
        if key in self._active_orders:
            self._active_orders[key]["status"] = "timeout_cancelled"

        # Send timeout notification
        if self.position_monitor and self.position_monitor._telegram:
            try:
                await self.position_monitor._telegram.send_message(
                    f"⏰ Paper LIMIT order for ${token_symbol} cancelled.\n"
                    f"Price did not reach limit within {timeout_minutes} minutes.\n"
                    f"Run /trade {token_symbol} to try again."
                )
            except Exception:
                pass

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

    def calculate_optimal_entry(self, signal: TradeSignal, token: Token) -> dict:
        """
        Calculate an optimal limit entry price based on the signal direction
        and recent price action. Instead of entering at market price, we try
        to get a better fill by placing the limit slightly into a
        support (for longs) or resistance (for shorts).

        Returns dict with:
          - limit_price: the recommended limit price
          - offset_pct: how far from market price (%)
          - reasoning: why this price was chosen
        """
        price = token.metrics.price
        if price <= 0:
            return {"limit_price": price, "offset_pct": 0, "reasoning": "No price data"}

        change_1h = abs(token.metrics.price_change_1h)
        change_5m = abs(token.metrics.price_change_5m)
        confidence = signal.confidence

        # Base offset: more volatile = bigger offset to catch a better fill
        # Higher confidence = smaller offset (enter quicker)
        if change_1h > 5:
            base_offset = 0.8  # Volatile — wait for 0.8% pullback
        elif change_1h > 2:
            base_offset = 0.5  # Normal volatility — 0.5% offset
        else:
            base_offset = 0.3  # Low volatility — tight 0.3% offset

        # Confidence adjustment
        if confidence >= 0.7:
            base_offset *= 0.6  # High confidence = enter closer to market
        elif confidence >= 0.5:
            base_offset *= 0.8
        # Low confidence keeps full offset

        # 5m momentum adjustment — if price is moving fast in our direction,
        # reduce offset so we don't miss the move
        if change_5m > 3:
            base_offset *= 0.5  # Price is running, get in quicker

        # Clamp
        base_offset = max(base_offset, 0.1)  # At least 0.1% better than market
        base_offset = min(base_offset, 1.5)  # No more than 1.5% away

        # Calculate limit price
        if signal.direction == Direction.LONG:
            # For longs: buy slightly below market (wait for a dip)
            limit_price = price * (1 - base_offset / 100)
            reasoning = f"Limit buy {base_offset:.1f}% below market for better long entry"
        else:
            # For shorts: sell slightly above market (wait for a bounce)
            limit_price = price * (1 + base_offset / 100)
            reasoning = f"Limit sell {base_offset:.1f}% above market for better short entry"

        # Round to appropriate precision
        if price < 0.01:
            limit_price = round(limit_price, 8)
        elif price < 1:
            limit_price = round(limit_price, 6)
        elif price < 100:
            limit_price = round(limit_price, 4)
        else:
            limit_price = round(limit_price, 2)

        return {
            "limit_price": limit_price,
            "offset_pct": round(base_offset, 2),
            "reasoning": reasoning,
            "market_price": price,
            "direction": signal.direction.value,
        }

    async def execute_limit_trade(self, signal: TradeSignal, token: Token,
                                   limit_price: float = None,
                                   exchange_name: str = None,
                                   timeout_minutes: int = 15,
                                   position_size_usd: float = None) -> dict:
        """
        Execute a trade using a LIMIT order instead of market.

        If limit_price is not provided, calculates optimal entry automatically.
        Places SL/TP after the limit order is filled.
        If not filled within timeout_minutes, cancels the order.
        """
        # Calculate optimal entry if not provided
        if limit_price is None:
            entry_calc = self.calculate_optimal_entry(signal, token)
            limit_price = entry_calc["limit_price"]
        else:
            entry_calc = {"limit_price": limit_price, "offset_pct": 0,
                         "reasoning": "User-specified limit price"}

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

        # Override entry price with our limit price
        adjusted_signal.risk.entry_price = limit_price

        # Recalculate SL/TP based on new entry
        sl_pct = abs(signal.risk.entry_price - signal.risk.stop_loss) / signal.risk.entry_price
        rr = signal.risk.risk_reward or 2.0

        if adjusted_signal.direction == Direction.LONG:
            adjusted_signal.risk.stop_loss = round(limit_price * (1 - sl_pct), 8)
            adjusted_signal.risk.take_profit_1 = round(limit_price * (1 + sl_pct * rr), 8)
            adjusted_signal.risk.take_profit_2 = round(limit_price * (1 + sl_pct * rr * 1.5), 8)
        else:
            adjusted_signal.risk.stop_loss = round(limit_price * (1 + sl_pct), 8)
            adjusted_signal.risk.take_profit_1 = round(limit_price * (1 - sl_pct * rr), 8)
            adjusted_signal.risk.take_profit_2 = round(limit_price * (1 - sl_pct * rr * 1.5), 8)

        # Save signal to database
        signal_data = {
            "token_symbol": adjusted_signal.token_symbol,
            "direction": adjusted_signal.direction.value,
            "confidence": adjusted_signal.confidence,
            "timeframe": adjusted_signal.timeframe,
            "entry_price": limit_price,
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
        except Exception as e:
            log.error("signal_save_error", error=str(e))
            signal_id = None

        # Paper trading mode — simulate limit order waiting for fill
        if self.paper_trading:
            log.info("paper_limit_trade_pending",
                    token=adjusted_signal.token_symbol,
                    direction=adjusted_signal.direction.value,
                    limit_price=limit_price,
                    market_price=token.metrics.price)

            # Store as pending paper limit order — NOT immediately filled
            self._active_orders[f"PAPER-LIMIT-{adjusted_signal.token_symbol}"] = {
                "order_id": f"PAPER-LIMIT-{signal_id or 'unknown'}",
                "signal": adjusted_signal,
                "token_symbol": adjusted_signal.token_symbol,
                "limit_price": limit_price,
                "market_price": token.metrics.price,
                "direction": adjusted_signal.direction.value,
                "status": "pending",
                "timeout_minutes": timeout_minutes,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # Start background monitoring for paper limit fill
            asyncio.create_task(
                self._monitor_paper_limit_order(
                    adjusted_signal, token, limit_price,
                    signal_id, timeout_minutes
                )
            )

            return {
                "executed": True,
                "mode": "paper",
                "order_type": "limit",
                "order_id": f"PAPER-LIMIT-{signal_id or 'unknown'}",
                "limit_price": limit_price,
                "market_price": token.metrics.price,
                "offset_pct": entry_calc.get("offset_pct", 0),
                "sl": adjusted_signal.risk.stop_loss,
                "tp1": adjusted_signal.risk.take_profit_1,
                "signal": adjusted_signal,
                "position_id": None,  # Not tracked until filled
                "warnings": risk_check.get("warnings", []),
                "message": f"📋 Paper LIMIT order PENDING at ${limit_price}. "
                           f"Monitoring price... auto-cancels in {timeout_minutes}min.",
            }

        # === LIVE LIMIT ORDER ===
        if not exchange_name:
            exchange_name = self._select_exchange(token)

        if exchange_name not in self._exchanges:
            return {
                "executed": False,
                "mode": "live",
                "reason": f"Exchange '{exchange_name}' not connected.",
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
                trade_value = 10.0

            symbol = f"{token.symbol}/USDT"
            leverage = adjusted_signal.risk.leverage or 3
            side = "buy" if adjusted_signal.direction == Direction.LONG else "sell"
            amount = trade_value / limit_price

            # Set leverage
            await self._set_leverage(exchange, exchange_name, symbol, leverage)

            # Place LIMIT order
            log.info("placing_limit_order",
                    exchange=exchange_name, symbol=symbol,
                    side=side, price=limit_price, amount=amount)

            order = await exchange.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=amount,
                price=limit_price,
                params=self._get_order_params(exchange_name, "limit"),
            )

            order_id = order.get("id", "unknown")

            log.info("limit_order_placed",
                    order_id=order_id, price=limit_price, amount=amount)

            # Store order info for monitoring
            self._active_orders[f"LIMIT-{token.symbol}"] = {
                "order_id": order_id,
                "exchange": exchange_name,
                "symbol": symbol,
                "side": side,
                "limit_price": limit_price,
                "amount": amount,
                "leverage": leverage,
                "direction": adjusted_signal.direction.value,
                "signal": adjusted_signal,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timeout_minutes": timeout_minutes,
                "status": "pending",
            }

            # Start monitoring in the background
            asyncio.create_task(
                self._monitor_limit_order(
                    token.symbol, order_id, exchange_name, symbol,
                    adjusted_signal, amount, timeout_minutes
                )
            )

            return {
                "executed": True,
                "mode": "live",
                "order_type": "limit",
                "exchange": exchange_name,
                "order_id": order_id,
                "limit_price": limit_price,
                "market_price": token.metrics.price,
                "offset_pct": entry_calc.get("offset_pct", 0),
                "amount": amount,
                "leverage": leverage,
                "timeout_minutes": timeout_minutes,
                "signal": adjusted_signal,
                "warnings": risk_check.get("warnings", []),
                "message": f"Limit order placed at ${limit_price}. "
                           f"Will auto-cancel in {timeout_minutes}min if not filled.",
            }

        except Exception as e:
            log.error("limit_order_failed",
                     token=adjusted_signal.token_symbol, error=str(e))
            return {
                "executed": False,
                "mode": "live",
                "order_type": "limit",
                "reason": str(e),
                "signal": adjusted_signal,
            }

    async def _monitor_limit_order(self, token_symbol: str, order_id: str,
                                    exchange_name: str, symbol: str,
                                    signal: TradeSignal, amount: float,
                                    timeout_minutes: int = 15) -> None:
        """
        Background task to monitor a limit order.
        - Checks every 30 seconds if it's been filled
        - If filled: places SL/TP orders and auto-tracks the position
        - If not filled after timeout: cancels the order
        """
        exchange = self._exchanges.get(exchange_name)
        if not exchange:
            return

        check_interval = 30  # seconds
        max_checks = (timeout_minutes * 60) // check_interval

        for i in range(max_checks):
            await asyncio.sleep(check_interval)

            try:
                order = await exchange.fetch_order(order_id, symbol)
                status = order.get("status", "open")

                if status == "closed":
                    # ORDER FILLED
                    fill_price = order.get("average") or order.get("price") or signal.risk.entry_price
                    filled_amount = order.get("filled") or amount

                    log.info("limit_order_filled",
                            token=token_symbol, price=fill_price,
                            amount=filled_amount)

                    # Place SL/TP
                    sl_id = await self._place_stop_loss(
                        exchange, exchange_name, symbol, signal, filled_amount
                    )
                    tp_id = await self._place_take_profit(
                        exchange, exchange_name, symbol, signal, filled_amount
                    )

                    # Track position
                    signal.executed = True
                    signal.execution_price = fill_price
                    signal.execution_time = datetime.now(timezone.utc)
                    self.risk_manager.record_trade_open(signal, None)
                    await self._auto_track_position(signal, exchange_name,
                                                    fill_price=fill_price)

                    # Update stored order
                    key = f"LIMIT-{token_symbol}"
                    if key in self._active_orders:
                        self._active_orders[key]["status"] = "filled"
                        # Move to regular active orders
                        self._active_orders[token_symbol] = {
                            "main_order": order_id,
                            "sl_order": sl_id,
                            "tp_order": tp_id,
                            "exchange": exchange_name,
                            "symbol": symbol,
                            "direction": signal.direction.value,
                            "amount": filled_amount,
                            "entry_price": fill_price,
                            "leverage": signal.risk.leverage or 3,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        del self._active_orders[key]

                    log.info("limit_order_complete",
                            token=token_symbol, sl=sl_id, tp=tp_id)
                    return

                elif status == "canceled":
                    log.info("limit_order_already_cancelled", token=token_symbol)
                    key = f"LIMIT-{token_symbol}"
                    if key in self._active_orders:
                        self._active_orders[key]["status"] = "cancelled"
                    return

            except Exception as e:
                log.warning("limit_order_check_failed",
                           token=token_symbol, error=str(e))

        # TIMEOUT — cancel the order
        try:
            await exchange.cancel_order(order_id, symbol)
            log.info("limit_order_timeout_cancelled",
                    token=token_symbol, minutes=timeout_minutes)

            key = f"LIMIT-{token_symbol}"
            if key in self._active_orders:
                self._active_orders[key]["status"] = "timeout_cancelled"

        except Exception as e:
            log.warning("limit_cancel_failed", token=token_symbol, error=str(e))

    def get_execution_status(self) -> dict:
        """Get current execution engine status."""
        # Count limit orders
        limit_orders = sum(
            1 for k, v in self._active_orders.items()
            if k.startswith("LIMIT-") and v.get("status") == "pending"
        )

        return {
            "paper_trading": self.paper_trading,
            "connected_exchanges": list(self._exchanges.keys()),
            "active_orders": len(self._active_orders),
            "pending_limit_orders": limit_orders,
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
