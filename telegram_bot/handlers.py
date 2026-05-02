"""
Telegram Bot Handlers

Command handlers, callback query handlers, and message handlers for the
Telegram bot interface. Handles user commands, refresh button callbacks,
trade signal generation, and manual token scan requests.
"""

import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from telegram_bot.formatters import (
    format_crime_pump_alert,
    format_refreshed_alert,
    format_no_signals,
    format_degraded_scan_notice,
    format_trade_analysis,
)
from telegram_bot.keyboards import (
    get_alert_keyboard,
    get_trade_analysis_keyboard,
    get_scan_summary_keyboard,
    get_watchlist_keyboard,
    get_positions_keyboard,
)
from utils.logger import get_logger
from utils.database import get_active_watchlist, add_to_watchlist, get_recent_alerts, get_performance_stats

log = get_logger("telegram_handlers")

# These will be set by the bot module when it initializes the scanner
_detector = None
_data_pipeline = None
_signal_aggregator = None
_execution_engine = None
_position_monitor = None
_backtester = None


def set_detector(detector):
    global _detector
    _detector = detector


def set_data_pipeline(pipeline):
    global _data_pipeline
    _data_pipeline = pipeline


def set_signal_aggregator(aggregator):
    global _signal_aggregator
    _signal_aggregator = aggregator


def set_execution_engine(engine):
    global _execution_engine
    _execution_engine = engine


def set_position_monitor(monitor):
    global _position_monitor
    _position_monitor = monitor


def set_backtester(backtester):
    global _backtester
    _backtester = backtester


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    welcome = (
        "Quil Crime Pump Detection Engine\n"
        "\n"
        "Monitoring DEX activity and derivatives markets for coordinated "
        "manipulation patterns across all major chains and exchanges.\n"
        "\n"
        "Commands:\n"
        "/scan [ticker] - Scan a token for crime pump signals\n"
        "/trade [ticker] - Full trade analysis with entry/SL/TP\n"
        "/analyze [ticker] - Alias for /trade\n"
        "/calc [ticker] [amount] [leverage] - Calculate profit/loss\n"
        "/track [ticker] [entry] [sl] [tp] - Track a position for alerts\n"
        "/positions - View all tracked positions\n"
        "/close [ticker] - Stop tracking a position\n"
        "/setalert [ticker] [price] - Set custom price alert\n"
        "/watch [ticker] - Add to monitoring watchlist\n"
        "/watchlist - View active watchlist\n"
        "/alerts - View recent alerts\n"
        "/risk - View current risk status\n"
        "/status - Bot status and data source health\n"
        "/sell [ticker] - Close a live exchange position\n"
        "/stats - Performance dashboard (win rate, PnL)\n"
        "/backtest [ticker] [days] - Test signals against history\n"
        "/help - Show all commands\n"
        "\n"
        "The bot also runs autonomously, scanning the market continuously "
        "and sending alerts when crime pump patterns are detected."
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command."""
    help_text = (
        "Quil Commands\n"
        "\n"
        "Detection:\n"
        "/scan [ticker] - Run a full crime pump analysis on a token. "
        "Evaluates all four detection layers and classifies the lifecycle stage.\n"
        "\n"
        "Trading:\n"
        "/trade [ticker] - Generate a complete trade signal with direction, "
        "entry price, stop loss, take profit levels, and leverage recommendation.\n"
        "/analyze [ticker] - Alias for /trade.\n"
        "/calc [ticker] [amount] [leverage] - Calculate potential profit/loss "
        "for a trade setup. Example: /calc ZAMA 100 5x\n"
        "\n"
        "Position Monitoring:\n"
        "/track [ticker] [entry] [sl] [tp] [leverage] - Start monitoring a "
        "position for alerts. Example: /track ZAMA 0.027 0.025 0.035 5x\n"
        "/positions - View all tracked positions with live PnL.\n"
        "/close [ticker] - Stop tracking a position.\n"
        "/setalert [ticker] [price] - Set a custom price alert.\n"
        "\n"
        "Watchlist:\n"
        "/watch [ticker] - Add a token to the monitoring watchlist.\n"
        "/watchlist - View all tokens being monitored.\n"
        "\n"
        "Info:\n"
        "/alerts [hours] - View recent crime pump alerts. Default 24 hours.\n"
        "/risk - View risk management status, open positions, circuit breaker.\n"
        "/status - Check bot health and data source availability.\n"
        "/sell [ticker] - Close a live exchange position.\n"
        "/stats - Performance dashboard with win rate, PnL, and trade history.\n"
        "/backtest [ticker] [period] - Test signals against historical data. "
        "Example: /backtest BTC 30d\n"
        "\n"
        "You can also just send a token ticker or contract address directly "
        "and the bot will run a scan.\n"
        "\n"
        "Every analysis output includes a refresh button. Tap it to re-scan "
        "that token in real time."
    )
    await update.message.reply_text(help_text)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /scan command. Runs a full crime pump analysis on the specified token.
    """
    if not context.args:
        await update.message.reply_text(
            "Please provide a token ticker or contract address.\n"
            "Example: /scan RAVE or /scan 0x1234..."
        )
        return

    query = context.args[0].upper().replace("$", "")
    log.info("manual_scan_requested", query=query, user=update.effective_user.id)

    # Send processing message
    processing_msg = await update.message.reply_text(
        f"Scanning ${query} across all four detection layers..."
    )

    try:
        if _data_pipeline is None or _detector is None:
            await processing_msg.edit_text(
                "Bot is still initializing. Please try again in a moment."
            )
            return

        # Build token data from all sources
        token = await _data_pipeline.build_token_profile(query)
        if token is None:
            await processing_msg.edit_text(
                f"Could not find data for ${query}. "
                f"Make sure the ticker or contract address is correct."
            )
            return

        # Fetch additional data for detection
        wallet_data = await _data_pipeline.get_wallet_analysis(token)
        derivatives_data = await _data_pipeline.get_derivatives_snapshot(token)
        funding_data = derivatives_data.get("funding_rates") if derivatives_data else None

        # Run the crime pump scan
        alert = await _detector.scan_token(
            token,
            wallet_data=wallet_data,
            derivatives_data=derivatives_data,
            funding_data=funding_data,
        )

        # Format and send response
        if alert.total_layers_triggered > 0:
            message = format_crime_pump_alert(alert)
            keyboard = get_alert_keyboard(alert.token_symbol, alert.contract_address)
        else:
            message = format_no_signals(query)
            keyboard = get_alert_keyboard(query)

        await processing_msg.edit_text(message, reply_markup=keyboard)

    except Exception as e:
        log.error("scan_error", query=query, error=str(e))
        await processing_msg.edit_text(
            f"Error scanning ${query}: {str(e)[:200]}"
        )


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /trade command. Generate a full trade analysis with
    entry, stop loss, take profit, leverage, and reasoning.

    This is the core trade signal generation command.
    """
    if not context.args:
        await update.message.reply_text(
            "Please provide a token ticker.\n"
            "Example: /trade RAVE"
        )
        return

    query = context.args[0].upper().replace("$", "")
    log.info("trade_analysis_requested", query=query, user=update.effective_user.id)

    processing_msg = await update.message.reply_text(
        f"Generating trade analysis for ${query}...\n"
        f"Running crime pump detection, trap bot, and technical analysis."
    )

    try:
        if _data_pipeline is None or _detector is None or _signal_aggregator is None:
            await processing_msg.edit_text(
                "Bot is still initializing. Please try again in a moment."
            )
            return

        # Build token profile
        token = await _data_pipeline.build_token_profile(query)
        if token is None:
            await processing_msg.edit_text(
                f"Could not find data for ${query}."
            )
            return

        # Run crime pump detection first
        wallet_data = await _data_pipeline.get_wallet_analysis(token)
        derivatives_data = await _data_pipeline.get_derivatives_snapshot(token)
        funding_data = derivatives_data.get("funding_rates") if derivatives_data else None

        crime_alert = await _detector.scan_token(
            token,
            wallet_data=wallet_data,
            derivatives_data=derivatives_data,
            funding_data=funding_data,
        )

        # Generate trade signal with all sources combined
        # Fetch multi-timeframe candle data for REAL chart analysis
        multi_tf_data = {}
        try:
            multi_tf_data = await _data_pipeline.fetch_multi_timeframe_candles(token.symbol)
        except Exception as e:
            log.warning("candle_fetch_failed", symbol=token.symbol, error=str(e))

        trade_signal = await _signal_aggregator.generate_trade_signal(
            token=token,
            crime_pump_alert=crime_alert if crime_alert.total_layers_triggered > 0 else None,
            multi_tf_data=multi_tf_data if multi_tf_data else None,
        )

        # Run risk validation if execution engine is available
        risk_result = None
        if _execution_engine:
            risk_result = _execution_engine.risk_manager.validate_trade(trade_signal, token)

        # Format the trade analysis
        message = format_trade_analysis(trade_signal)

        # Add risk warnings if any
        if risk_result and risk_result.get("warnings"):
            message += "\n\nRisk Warnings:"
            for w in risk_result["warnings"]:
                message += f"\n  ⚠️ {w}"

        if risk_result and not risk_result.get("approved"):
            message += "\n\nTrade NOT approved by risk manager:"
            for r in risk_result.get("rejections", []):
                message += f"\n  🚫 {r}"

        # Add execution mode info
        if _execution_engine:
            mode = "Paper Trading" if _execution_engine.paper_trading else "Live Trading"
            message += f"\n\nExecution Mode: {mode}"

        # Add data source info
        data_sources = []
        if multi_tf_data:
            tf_info = ", ".join(f"{tf}({len(c)})" for tf, c in multi_tf_data.items())
            source = "Bybit" if any(c and c[0].get("volume", 0) > 0 for c in multi_tf_data.values()) else "CoinGecko"
            data_sources.append(f"Candles: {source} [{tf_info}]")
        else:
            data_sources.append("Candles: None (DexScreener price only)")

        if data_sources:
            message += f"\nData: {' | '.join(data_sources)}"

        keyboard = get_trade_analysis_keyboard(query)
        await processing_msg.edit_text(message, reply_markup=keyboard)

    except Exception as e:
        log.error("trade_analysis_error", query=query, error=str(e))
        await processing_msg.edit_text(
            f"Error generating trade analysis for ${query}: {str(e)[:200]}"
        )


async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /risk command. Show current risk management status."""
    if _execution_engine is None:
        await update.message.reply_text("Execution engine not initialized.")
        return

    risk_summary = _execution_engine.risk_manager.get_risk_summary()
    exec_status = _execution_engine.get_execution_status()

    lines = [
        "📊 Risk Management Status\n",
        f"Portfolio Value: ${risk_summary['portfolio_value']:,.2f}",
        f"Open Positions: {risk_summary['open_positions']}",
        f"Daily PnL: ${risk_summary['daily_pnl']:+,.2f} "
        f"({risk_summary['daily_pnl_pct']:+.1f}%)",
    ]

    if risk_summary["circuit_breaker_active"]:
        lines.append("\n🚫 CIRCUIT BREAKER ACTIVE - No new trades allowed")
    else:
        lines.append("\n✅ Circuit breaker: Clear")

    lines.append(f"\nExecution Mode: {'Paper' if exec_status['paper_trading'] else 'Live'}")

    if exec_status["connected_exchanges"]:
        lines.append(f"Connected Exchanges: {', '.join(exec_status['connected_exchanges'])}")
    else:
        lines.append("Connected Exchanges: None")

    await update.message.reply_text("\n".join(lines))


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /watch command. Add a token to the monitoring watchlist."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a token ticker.\n"
            "Example: /watch RAVE"
        )
        return

    ticker = context.args[0].upper().replace("$", "")
    contract = context.args[1] if len(context.args) > 1 else None

    try:
        result = await add_to_watchlist(ticker, contract_address=contract, reason="User added")

        if result["status"] == "already_exists":
            await update.message.reply_text(
                f"${ticker} is already in the watchlist. "
                f"Use /watchlist to view all monitored tokens."
            )
        elif result["status"] == "reactivated":
            await update.message.reply_text(
                f"${ticker} re-added to the monitoring watchlist. "
                f"Crime pump scanning resumed."
            )
        else:
            await update.message.reply_text(
                f"${ticker} added to the monitoring watchlist. "
                f"The bot will continuously scan for crime pump signals."
            )
    except Exception as e:
        log.error("watchlist_add_error", ticker=ticker, error=str(e))
        await update.message.reply_text(f"Error adding to watchlist: {str(e)[:200]}")


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /watchlist command. Show all actively monitored tokens."""
    try:
        watchlist = await get_active_watchlist()
        if not watchlist:
            await update.message.reply_text("Watchlist is empty. Use /watch [ticker] to add tokens.")
            return

        lines = ["📋 Active Watchlist\n"]
        for item in watchlist:
            symbol = item.get("token_symbol", "?")
            added = item.get("added_at", "")
            lines.append(f"  ${symbol} (added {added})")

        lines.append(f"\n{len(watchlist)} tokens monitored")

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=get_watchlist_keyboard()
        )
    except Exception as e:
        log.error("watchlist_view_error", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /alerts command. Show recent crime pump alerts."""
    hours = 24
    if context.args:
        try:
            hours = int(context.args[0])
        except ValueError:
            pass

    try:
        alerts = await get_recent_alerts(hours=hours)
        if not alerts:
            await update.message.reply_text(
                f"No crime pump alerts in the last {hours} hours."
            )
            return

        lines = [f"🚨 Recent Alerts (last {hours}h)\n"]
        for a in alerts[:10]:
            symbol = a.get("token_symbol", "?")
            confidence = a.get("confidence_level", "?")
            stage = a.get("crime_stage", "?")
            time = a.get("created_at", "")
            lines.append(f"  ${symbol} - {confidence} - {stage} ({time})")

        lines.append(f"\n{len(alerts)} total alerts")

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.error("alerts_view_error", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /status command. Show bot health and data source status."""
    status_lines = [
        "🟢 Quil Status\n",
        "Data Sources:",
        "  DexScreener: 🟢 Online (no key required)",
    ]

    # Check configured API keys
    from config.settings import (
        COINGLASS_API_KEY, ETHERSCAN_API_KEY, BSCSCAN_API_KEY,
        SOLSCAN_API_KEY, TWITTER_BEARER_TOKEN,
    )

    coinglass = "🟢 Configured" if COINGLASS_API_KEY else "🔴 Not configured"
    etherscan = "🟢 Configured" if ETHERSCAN_API_KEY else "🔴 Not configured"
    bscscan = "🟢 Configured" if BSCSCAN_API_KEY else "🔴 Not configured"
    solscan = "🟢 Configured" if SOLSCAN_API_KEY else "🔴 Not configured"
    twitter = "🟢 Configured" if TWITTER_BEARER_TOKEN else "🔴 Not configured"

    status_lines.extend([
        f"  Coinglass: {coinglass}",
        f"  Etherscan: {etherscan}",
        f"  BscScan: {bscscan}",
        f"  Solscan: {solscan}",
        f"  Twitter/X: {twitter}",
    ])

    if _detector:
        status_lines.append("\nDetection Engine: 🟢 Active")
    else:
        status_lines.append("\nDetection Engine: 🟡 Initializing")

    if _signal_aggregator:
        status_lines.append("Signal Aggregator: 🟢 Active")

    if _execution_engine:
        mode = "Paper" if _execution_engine.paper_trading else "Live"
        status_lines.append(f"Execution Engine: 🟢 Active ({mode} mode)")

    await update.message.reply_text("\n".join(status_lines))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    query = update.callback_query
    await query.answer()

    data = query.data
    log.info("callback_received", data=data)

    if data.startswith("refresh:"):
        token_symbol = data.split(":", 1)[1]
        await _handle_refresh(query, token_symbol)

    elif data.startswith("refresh_trade:"):
        token_symbol = data.split(":", 1)[1]
        await _handle_trade_analysis(query, token_symbol)

    elif data.startswith("trade_analysis:"):
        token_symbol = data.split(":", 1)[1]
        await _handle_trade_analysis(query, token_symbol)

    elif data.startswith("crime_check:"):
        token_symbol = data.split(":", 1)[1]
        await _handle_refresh(query, token_symbol)

    elif data.startswith("execute_trade:"):
        token_symbol = data.split(":", 1)[1]
        await _handle_execute_trade(query, token_symbol)

    elif data.startswith("confirm_trade:"):
        confirm_id = data.split(":", 1)[1]
        await _handle_confirm_trade(query, confirm_id)

    elif data.startswith("cancel_trade:"):
        await query.edit_message_text("❌ Trade cancelled.")

    elif data.startswith("confirm_limit:"):
        confirm_id = data.split(":", 1)[1]
        await _handle_confirm_limit_trade(query, confirm_id)

    elif data.startswith("modify_amount:"):
        confirm_id = data.split(":", 1)[1]
        await _handle_modify_amount(query, confirm_id)

    elif data.startswith("modify_leverage:"):
        confirm_id = data.split(":", 1)[1]
        await _handle_modify_leverage(query, confirm_id)

    elif data.startswith("modify_sl:") or data.startswith("modify_tp:") or data.startswith("modify_entry:"):
        confirm_id = data.split(":", 1)[1]
        await query.edit_message_text(
            "📝 To set a custom SL/TP/Entry, use the command:\n\n"
            f"/set {data.split(':')[0].replace('modify_', '')} [price] {confirm_id}\n\n"
            "Example: /set sl 75000 " + confirm_id + "\n\n"
            "Or tap Back to return.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"back_confirm:{confirm_id}")]
            ])
        )

    elif data.startswith("set_amount:"):
        parts = data.split(":")
        amount = float(parts[1])
        confirm_id = parts[2]
        pending = _execution_engine.get_pending_trade(confirm_id)
        if pending:
            pending["custom_amount"] = amount
            await _show_confirmation_screen(query, confirm_id)
        else:
            await query.edit_message_text("⏰ Trade expired.")

    elif data.startswith("set_leverage:"):
        parts = data.split(":")
        lev = int(parts[1])
        confirm_id = parts[2]
        pending = _execution_engine.get_pending_trade(confirm_id)
        if pending:
            pending["signal"].risk.leverage = lev
            await _show_confirmation_screen(query, confirm_id)
        else:
            await query.edit_message_text("⏰ Trade expired.")

    elif data.startswith("custom_amount:"):
        confirm_id = data.split(":", 1)[1]
        await query.edit_message_text(
            "✏️ Type your custom amount:\n\n"
            f"`/set amount [your amount] {confirm_id}`\n\n"
            "Example: `/set amount 75 " + confirm_id + "`\n\n"
            "This sets the trade to use exactly that $ amount.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"modify_amount:{confirm_id}")]
            ])
        )

    elif data.startswith("custom_leverage:"):
        confirm_id = data.split(":", 1)[1]
        await query.edit_message_text(
            "✏️ Type your custom leverage:\n\n"
            f"`/set leverage [multiplier] {confirm_id}`\n\n"
            "Example: `/set leverage 7 " + confirm_id + "`\n\n"
            "Max: 20x. Higher = more risk.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data=f"modify_leverage:{confirm_id}")]
            ])
        )

    elif data.startswith("back_confirm:"):
        confirm_id = data.split(":", 1)[1]
        await _show_confirmation_screen(query, confirm_id)

    elif data == "refresh_positions":
        await _handle_refresh_positions(query)

    elif data.startswith("watchlist_add:"):
        token_symbol = data.split(":", 1)[1]
        try:
            result = await add_to_watchlist(token_symbol, reason="Added via button")
            if result["status"] == "already_exists":
                msg = f"\n\n⚠️ ${token_symbol} is already in the watchlist"
            elif result["status"] == "reactivated":
                msg = f"\n\n✅ ${token_symbol} re-added to watchlist"
            else:
                msg = f"\n\n✅ ${token_symbol} added to watchlist"
            await query.edit_message_text(
                query.message.text + msg,
                reply_markup=query.message.reply_markup,
            )
        except Exception as e:
            log.error("watchlist_button_error", error=str(e))

    elif data == "force_rescan":
        await query.edit_message_text("Forcing a full market rescan...")

    elif data == "view_watchlist":
        watchlist = await get_active_watchlist()
        if watchlist:
            items = [f"${w['token_symbol']}" for w in watchlist]
            await query.edit_message_text(
                "📋 Watchlist: " + ", ".join(items),
                reply_markup=get_watchlist_keyboard(),
            )
        else:
            await query.edit_message_text("Watchlist is empty.")


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /sell command. Close a position on the exchange."""
    if not context.args:
        await update.message.reply_text(
            "Please specify the token to sell.\n"
            "Example: /sell ZAMA"
        )
        return

    ticker = context.args[0].upper().replace("$", "")

    if _execution_engine is None:
        await update.message.reply_text("Execution engine not initialized.")
        return

    try:
        await update.message.reply_text(f"Closing ${ticker} position...")

        result = await _execution_engine.close_position(ticker)

        if result.get("closed"):
            mode = result.get("mode", "unknown")
            if mode == "paper":
                await update.message.reply_text(
                    f"📝 Paper Position Closed\n\n"
                    f"${ticker} paper position has been closed."
                )
            else:
                entry = result.get("entry_price", 0)
                close = result.get("close_price", 0)
                pnl = result.get("pnl_pct_leveraged", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"

                await update.message.reply_text(
                    f"✅ Position Closed — ${ticker}\n\n"
                    f"Exchange: {result.get('exchange', 'unknown')}\n"
                    f"Entry: ${entry:,.6f}\n"
                    f"Close: ${close:,.6f}\n"
                    f"PnL: {emoji} {pnl:+.2f}%\n"
                    f"Order: {result.get('order_id', 'N/A')}"
                )
        else:
            reason = result.get("reason", "Unknown error")
            await update.message.reply_text(
                f"❌ Could not close ${ticker}\n\n"
                f"Reason: {reason}"
            )

    except Exception as e:
        log.error("sell_command_error", ticker=ticker, error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


def _build_confirmation_message(signal, token, confirm_id, execution_engine):
    """Build the trade confirmation message with current parameters."""
    from config.settings import PORTFOLIO_VALUE

    mode = "📝 PAPER" if execution_engine.paper_trading else "🔴 LIVE"
    direction_emoji = "📈" if signal.direction.value == "long" else "📉"
    direction = signal.direction.value.upper()

    entry = signal.risk.entry_price
    sl = signal.risk.stop_loss
    tp1 = signal.risk.take_profit_1
    tp2 = signal.risk.take_profit_2
    leverage = signal.risk.leverage or 3
    rr = signal.risk.risk_reward
    confidence = signal.confidence
    sl_pct = abs(entry - sl) / entry * 100 if entry > 0 else 0

    price_fmt = lambda p: f"${p:,.6f}" if p < 1 else f"${p:,.2f}"

    # Check if custom amount was set
    pending = execution_engine.get_pending_trade(confirm_id)
    custom_amount = pending.get("custom_amount") if pending else None

    if custom_amount:
        order_cost = custom_amount
    else:
        pos_size_pct = signal.risk.position_size_pct
        portfolio = execution_engine.risk_manager.portfolio_value
        order_cost = pos_size_pct * portfolio / 100

    # Liq price estimate
    mmr = 0.025
    fee = 0.0006
    imr = 1.0 / leverage
    if direction == "LONG":
        liq_price = entry * (1 - imr + mmr + fee)
    else:
        liq_price = entry * (1 + imr - mmr - fee)

    # Optimal limit entry
    limit_calc = execution_engine.calculate_optimal_entry(signal, token)
    limit_price = limit_calc["limit_price"]
    limit_offset = limit_calc["offset_pct"]

    confirm_msg = (
        f"⚠️ TRADE CONFIRMATION ({mode})\n"
        f"{'=' * 30}\n\n"
        f"Token: ${signal.token_symbol}\n"
        f"Direction: {direction_emoji} {direction}\n\n"
        f"💰 Order Details:\n"
        f"  Market Price: {price_fmt(entry)}\n"
        f"  Limit Price: {price_fmt(limit_price)} ({limit_offset:.1f}% better)\n"
        f"  💵 Amount: ${order_cost:.2f} USDT"
    )

    if custom_amount:
        confirm_msg += " (custom)\n"
    else:
        confirm_msg += f" ({signal.risk.position_size_pct:.1f}% of portfolio)\n"

    confirm_msg += (
        f"  Leverage: {leverage}x (Isolated)\n\n"
        f"🛡️ Risk Management:\n"
        f"  Stop Loss: {price_fmt(sl)} ({sl_pct:.1f}% away)\n"
        f"  Take Profit 1: {price_fmt(tp1)}\n"
        f"  Take Profit 2: {price_fmt(tp2)}\n"
        f"  Risk/Reward: {rr:.1f}:1\n\n"
        f"📊 Position Info:\n"
        f"  Est. Liq. Price: {price_fmt(liq_price)}\n"
        f"  Confidence: {confidence:.0%}\n\n"
        f"Tap a button below to modify, or proceed:\n"
    )

    if execution_engine.paper_trading:
        confirm_msg += "📝 This is a PAPER trade."
    else:
        confirm_msg += "🔴 REAL order — real money at risk!"

    return confirm_msg


async def _handle_execute_trade(query, token_symbol: str) -> None:
    """Handle the 'Execute Trade' button. Shows confirmation with modify buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if _execution_engine is None or _signal_aggregator is None:
        await query.edit_message_text("Bot is still initializing.")
        return

    try:
        if _data_pipeline is None:
            await query.edit_message_text("Data pipeline not available.")
            return

        await query.edit_message_text(f"Preparing trade for ${token_symbol}...")

        token = await _data_pipeline.build_token_profile(token_symbol)
        if token is None:
            await query.edit_message_text(f"Could not find ${token_symbol}")
            return

        # Fetch multi-timeframe candle data for chart analysis
        multi_tf_data = {}
        try:
            multi_tf_data = await _data_pipeline.fetch_multi_timeframe_candles(token_symbol)
        except Exception:
            pass

        signal = await _signal_aggregator.generate_trade_signal(
            token=token,
            multi_tf_data=multi_tf_data if multi_tf_data else None,
        )
        if signal is None:
            await query.edit_message_text("Could not generate trade signal.")
            return

        # Store pending trade for confirmation
        confirm_id = _execution_engine.store_pending_trade(
            token_symbol, signal, token
        )

        # Show the interactive confirmation screen
        await _show_confirmation_screen(query, confirm_id)

    except Exception as e:
        log.error("execute_trade_error", token=token_symbol, error=str(e))
        await query.edit_message_text(f"Error preparing trade: {str(e)[:200]}")


async def _show_confirmation_screen(query, confirm_id: str) -> None:
    """Show the confirmation screen with modify buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pending = _execution_engine.get_pending_trade(confirm_id)
    if not pending:
        await query.edit_message_text("⏰ Trade expired. Run /trade again.")
        return

    signal = pending["signal"]
    token = pending["token"]

    confirm_msg = _build_confirmation_message(
        signal, token, confirm_id, _execution_engine
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 Amount", callback_data=f"modify_amount:{confirm_id}"),
            InlineKeyboardButton("⚡ Leverage", callback_data=f"modify_leverage:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("🔴 Stop Loss", callback_data=f"modify_sl:{confirm_id}"),
            InlineKeyboardButton("🟢 Take Profit", callback_data=f"modify_tp:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("📍 Entry Price", callback_data=f"modify_entry:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("⚡ Market Order", callback_data=f"confirm_trade:{confirm_id}"),
            InlineKeyboardButton("📊 Limit Order", callback_data=f"confirm_limit:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_trade:{confirm_id}"),
        ],
    ])

    await query.edit_message_text(confirm_msg, reply_markup=buttons)


async def _handle_modify_amount(query, confirm_id: str) -> None:
    """Show amount selection buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pending = _execution_engine.get_pending_trade(confirm_id)
    if not pending:
        await query.edit_message_text("⏰ Trade expired. Run /trade again.")
        return

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("$5", callback_data=f"set_amount:5:{confirm_id}"),
            InlineKeyboardButton("$10", callback_data=f"set_amount:10:{confirm_id}"),
            InlineKeyboardButton("$25", callback_data=f"set_amount:25:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("$50", callback_data=f"set_amount:50:{confirm_id}"),
            InlineKeyboardButton("$100", callback_data=f"set_amount:100:{confirm_id}"),
            InlineKeyboardButton("$250", callback_data=f"set_amount:250:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("$500", callback_data=f"set_amount:500:{confirm_id}"),
            InlineKeyboardButton("$1000", callback_data=f"set_amount:1000:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Custom Amount", callback_data=f"custom_amount:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=f"back_confirm:{confirm_id}"),
        ],
    ])

    await query.edit_message_text(
        "💵 Select trade amount (USDT):\n\n"
        "Pick a preset or tap Custom to enter your own.",
        reply_markup=buttons
    )


async def _handle_modify_leverage(query, confirm_id: str) -> None:
    """Show leverage selection buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    pending = _execution_engine.get_pending_trade(confirm_id)
    if not pending:
        await query.edit_message_text("⏰ Trade expired. Run /trade again.")
        return

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1x", callback_data=f"set_leverage:1:{confirm_id}"),
            InlineKeyboardButton("2x", callback_data=f"set_leverage:2:{confirm_id}"),
            InlineKeyboardButton("3x", callback_data=f"set_leverage:3:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("5x", callback_data=f"set_leverage:5:{confirm_id}"),
            InlineKeyboardButton("10x", callback_data=f"set_leverage:10:{confirm_id}"),
            InlineKeyboardButton("20x", callback_data=f"set_leverage:20:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Custom Leverage", callback_data=f"custom_leverage:{confirm_id}"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=f"back_confirm:{confirm_id}"),
        ],
    ])

    await query.edit_message_text(
        "⚡ Select leverage:\n\n"
        "Pick a preset or tap Custom to enter your own.\n"
        "⚠️ Higher leverage = higher risk of liquidation.",
        reply_markup=buttons
    )


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /set command to modify pending trade parameters.

    Usage:
        /set sl 75000 abc12345
        /set tp 80000 abc12345
        /set entry 76000 abc12345
        /set amount 75 abc12345
        /set leverage 7 abc12345
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /set [param] [value] [confirm_id]\n\n"
            "Params: sl, tp, entry, amount, leverage\n"
            "Example: /set sl 75000 abc12345"
        )
        return

    if _execution_engine is None:
        await update.message.reply_text("Bot not initialized.")
        return

    param = context.args[0].lower()
    try:
        value = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid value. Use a number.")
        return

    confirm_id = context.args[2]

    pending = _execution_engine.get_pending_trade(confirm_id)
    if not pending:
        await update.message.reply_text("⏰ Trade expired. Run /trade again.")
        return

    signal = pending["signal"]

    if param == "sl":
        signal.risk.stop_loss = value
        label = "Stop Loss"
        display = f"${value:,.6f}" if value < 1 else f"${value:,.2f}"
    elif param == "tp":
        signal.risk.take_profit_1 = value
        label = "Take Profit 1"
        display = f"${value:,.6f}" if value < 1 else f"${value:,.2f}"
    elif param == "entry":
        signal.risk.entry_price = value
        label = "Entry Price"
        display = f"${value:,.6f}" if value < 1 else f"${value:,.2f}"
    elif param == "amount":
        pending["custom_amount"] = value
        label = "Trade Amount"
        display = f"${value:,.2f} USDT"
    elif param in ("leverage", "lev"):
        lev = min(int(value), 20)
        signal.risk.leverage = lev
        label = "Leverage"
        display = f"{lev}x"
    else:
        await update.message.reply_text(
            "Unknown parameter. Use: sl, tp, entry, amount, or leverage"
        )
        return

    # Recalculate risk/reward if price params changed
    if param in ("sl", "tp", "entry"):
        entry = signal.risk.entry_price
        sl = signal.risk.stop_loss
        tp1 = signal.risk.take_profit_1
        if entry > 0 and sl > 0 and abs(entry - sl) > 0:
            signal.risk.risk_reward = abs(tp1 - entry) / abs(entry - sl)

    # Send confirmation and show a button to go back
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Back to Confirmation", callback_data=f"back_confirm:{confirm_id}")]
    ])

    await update.message.reply_text(
        f"✅ {label} updated to {display}\n\n"
        "Tap below to return to the trade confirmation.",
        reply_markup=buttons
    )


async def _handle_confirm_trade(query, confirm_id: str) -> None:
    """Handle trade confirmation. Actually executes the trade."""
    if _execution_engine is None:
        await query.edit_message_text("Execution engine not available.")
        return

    pending = _execution_engine.consume_pending_trade(confirm_id)
    if not pending:
        await query.edit_message_text(
            "⏰ Trade confirmation expired (5 minute limit). "
            "Run /trade again to get a fresh signal."
        )
        return

    signal = pending["signal"]
    token = pending["token"]
    custom_amount = pending.get("custom_amount")

    try:
        await query.edit_message_text(
            f"⏳ Executing {signal.direction.value.upper()} on ${signal.token_symbol}..."
        )

        result = await _execution_engine.execute_trade(
            signal, token, position_size_usd=custom_amount
        )

        if result.get("executed"):
            mode = result["mode"]
            if mode == "paper":
                msg = (
                    f"📝 Paper Trade Executed\n\n"
                    f"Token: ${signal.token_symbol}\n"
                    f"Direction: {signal.direction.value.upper()}\n"
                    f"Entry: ${signal.risk.entry_price:,.8f}\n"
                    f"Stop Loss: ${signal.risk.stop_loss:,.8f}\n"
                    f"TP1: ${signal.risk.take_profit_1:,.8f}\n"
                    f"Order: {result.get('order_id', 'N/A')}\n\n"
                    f"Position auto-tracked. Use /positions to monitor."
                )
            else:
                msg = (
                    f"✅ LIVE Trade Executed\n\n"
                    f"Exchange: {result.get('exchange', 'unknown')}\n"
                    f"Token: ${signal.token_symbol}\n"
                    f"Direction: {signal.direction.value.upper()}\n"
                    f"Entry: ${result.get('price', 0):,.8f}\n"
                    f"Amount: {result.get('amount', 0):,.4f}\n"
                    f"Leverage: {result.get('leverage', 0)}x\n"
                    f"Order: {result.get('order_id', 'N/A')}\n"
                    f"SL Order: {result.get('sl_order', 'N/A')}\n"
                    f"TP Order: {result.get('tp_order', 'N/A')}\n\n"
                    f"Position auto-tracked. Use /positions to monitor.\n"
                    f"Use /sell {signal.token_symbol} to close."
                )

            warnings = result.get("warnings", [])
            if warnings:
                msg += "\n⚠️ Warnings:\n" + "\n".join(f"- {w}" for w in warnings)

            await query.edit_message_text(msg)
        else:
            reason = result.get("reason", "Unknown error")
            rejections = result.get("rejections", [])
            msg = f"❌ Trade NOT Executed\n\nReason: {reason}"
            if rejections:
                msg += "\n\nRejections:\n" + "\n".join(f"- {r}" for r in rejections)
            await query.edit_message_text(msg)

    except Exception as e:
        log.error("confirm_trade_error", error=str(e))
        await query.edit_message_text(f"❌ Execution failed: {str(e)[:200]}")


async def _handle_confirm_limit_trade(query, confirm_id: str) -> None:
    """Handle limit order confirmation. Places a limit order instead of market."""
    if _execution_engine is None:
        await query.edit_message_text("Execution engine not initialized.")
        return

    try:
        pending = _execution_engine.consume_pending_trade(confirm_id)
        if pending is None:
            await query.edit_message_text(
                "⏰ Trade confirmation expired (5 minute limit). "
                "Run /trade again to get a fresh signal."
            )
            return

        signal = pending["signal"]
        token = pending["token"]
        custom_amount = pending.get("custom_amount")

        await query.edit_message_text(
            f"📊 Placing LIMIT order for ${signal.token_symbol}...\n"
            f"Calculating optimal entry..."
        )

        result = await _execution_engine.execute_limit_trade(
            signal, token, position_size_usd=custom_amount
        )

        if result.get("executed"):
            mode = result.get("mode", "paper")
            limit_price = result.get("limit_price", 0)
            market_price = result.get("market_price", 0)
            offset = result.get("offset_pct", 0)

            price_fmt = lambda p: f"${p:,.6f}" if p < 1 else f"${p:,.2f}"

            if mode == "paper":
                timeout = result.get("signal", signal)
                msg = (
                    f"📋 Paper LIMIT Order PENDING\n\n"
                    f"Token: ${signal.token_symbol}\n"
                    f"Direction: {signal.direction.value.upper()}\n"
                    f"Limit Price: {price_fmt(limit_price)}\n"
                    f"Market Price: {price_fmt(market_price)}\n"
                    f"Offset: {offset:.1f}% better entry\n"
                    f"SL: {price_fmt(result.get('sl', 0))}\n"
                    f"TP: {price_fmt(result.get('tp1', 0))}\n\n"
                    f"⏳ Waiting for price to hit limit...\n"
                    f"Auto-cancels in 15 min if not filled.\n"
                    f"You'll get a notification when filled."
                )
            else:
                timeout = result.get("timeout_minutes", 15)
                msg = (
                    f"📊 LIVE Limit Order Placed\n\n"
                    f"Exchange: {result.get('exchange', 'unknown')}\n"
                    f"Token: ${signal.token_symbol}\n"
                    f"Direction: {signal.direction.value.upper()}\n"
                    f"Limit Price: {price_fmt(limit_price)}\n"
                    f"Market Price: {price_fmt(market_price)}\n"
                    f"Offset: {offset:.1f}% better entry\n"
                    f"Amount: {result.get('amount', 0):,.4f}\n"
                    f"Leverage: {result.get('leverage', 0)}x\n"
                    f"Order ID: {result.get('order_id', 'N/A')}\n\n"
                    f"⏱️ Auto-cancels in {timeout} min if not filled.\n"
                    f"SL/TP will be placed automatically after fill.\n"
                    f"Use /positions to monitor."
                )

            warnings = result.get("warnings", [])
            if warnings:
                msg += "\n⚠️ Warnings:\n" + "\n".join(f"- {w}" for w in warnings)

            await query.edit_message_text(msg)
        else:
            reason = result.get("reason", "Unknown error")
            rejections = result.get("rejections", [])
            msg = f"❌ Limit Order NOT Placed\n\nReason: {reason}"
            if rejections:
                msg += "\n\nRejections:\n" + "\n".join(f"- {r}" for r in rejections)
            await query.edit_message_text(msg)

    except Exception as e:
        log.error("confirm_limit_trade_error", error=str(e))
        await query.edit_message_text(f"❌ Limit order failed: {str(e)[:200]}")


async def _handle_refresh_positions(query) -> None:
    """Handle the refresh button on /positions. Re-fetches and displays current positions."""
    if _position_monitor is None:
        await query.edit_message_text("Position monitor not initialized.")
        return

    try:
        from utils.database import get_active_positions as db_get_positions

        # Query database first
        try:
            db_positions = await db_get_positions()
        except Exception:
            db_positions = []

        mem_positions = _position_monitor.active_positions if _position_monitor else []
        mem_by_symbol = {p.symbol: p for p in mem_positions}

        # Sync DB → memory
        if _position_monitor and db_positions:
            for dbp in db_positions:
                sym = dbp.get("symbol", "")
                if sym not in mem_by_symbol:
                    _position_monitor.add_position(
                        symbol=sym,
                        direction=dbp.get("direction", "LONG").upper(),
                        entry_price=dbp.get("entry_price", 0),
                        stop_loss=dbp.get("stop_loss", 0),
                        take_profit_1=dbp.get("take_profit_1", 0),
                        take_profit_2=dbp.get("take_profit_2", 0),
                        leverage=dbp.get("leverage", 1),
                        size_usd=dbp.get("size_usd", 0),
                    )
            mem_positions = _position_monitor.active_positions
            mem_by_symbol = {p.symbol: p for p in mem_positions}

        if not db_positions and not mem_positions:
            await query.edit_message_text(
                "No active positions being tracked.\n"
                "Use /track [ticker] [entry] [sl] [tp] to start monitoring."
            )
            return

        # Build from DB + memory
        positions_to_show = []
        for dbp in db_positions:
            sym = dbp.get("symbol", "")
            mem_pos = mem_by_symbol.get(sym)
            positions_to_show.append({
                "symbol": sym,
                "direction": dbp.get("direction", "LONG").upper(),
                "entry_price": dbp.get("entry_price", 0),
                "current_price": mem_pos.current_price if mem_pos else dbp.get("entry_price", 0),
                "pnl_pct": mem_pos.pnl_pct if mem_pos else 0,
                "leverage": dbp.get("leverage", 1),
                "stop_loss": dbp.get("stop_loss", 0),
                "take_profit_1": dbp.get("take_profit_1", 0),
            })

        db_symbols = {p.get("symbol", "") for p in db_positions}
        for mp in mem_positions:
            if mp.symbol not in db_symbols:
                positions_to_show.append({
                    "symbol": mp.symbol, "direction": mp.direction,
                    "entry_price": mp.entry_price, "current_price": mp.current_price,
                    "pnl_pct": mp.pnl_pct, "leverage": mp.leverage,
                    "stop_loss": mp.stop_loss, "take_profit_1": mp.take_profit_1,
                })

        lines = [f"📋 Tracked Positions ({len(positions_to_show)})\n"]

        for pos in positions_to_show:
            entry = pos["entry_price"]
            current = pos["current_price"]
            pnl = pos["pnl_pct"]
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            entry_str = f"${entry:.6f}" if entry < 1 else f"${entry:,.2f}"
            current_str = f"${current:.6f}" if current < 1 else f"${current:,.2f}"

            lines.append(f"{pnl_emoji} ${pos['symbol']} ({pos['direction']})")
            lines.append(f"  Entry: {entry_str}")
            lines.append(f"  Current: {current_str}")
            lines.append(f"  PnL: {pnl:+.1f}%")
            if pos["leverage"] > 1:
                lines.append(f"  Leverage: {pos['leverage']}x")
            if pos["stop_loss"] > 0:
                sl = pos["stop_loss"]
                sl_str = f"${sl:.6f}" if sl < 1 else f"${sl:,.2f}"
                lines.append(f"  SL: {sl_str}")
            if pos["take_profit_1"] > 0:
                tp = pos["take_profit_1"]
                tp_str = f"${tp:.6f}" if tp < 1 else f"${tp:,.2f}"
                lines.append(f"  TP: {tp_str}")
            lines.append("")

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        lines.append(f"🕐 Updated: {now}")

        keyboard = get_positions_keyboard()
        await query.edit_message_text("\n".join(lines), reply_markup=keyboard)

    except Exception as e:
        log.error("refresh_positions_error", error=str(e))
        await query.edit_message_text(f"Error refreshing positions: {str(e)[:200]}")


async def _handle_refresh(query, token_symbol: str) -> None:
    """Handle the refresh button press. Re-run analysis and highlight changes."""
    await query.edit_message_text(f"Refreshing analysis for ${token_symbol}...")

    try:
        if _data_pipeline is None or _detector is None:
            await query.edit_message_text("Bot is still initializing.")
            return

        token = await _data_pipeline.build_token_profile(token_symbol)
        if token is None:
            await query.edit_message_text(f"Could not find data for ${token_symbol}")
            return

        wallet_data = await _data_pipeline.get_wallet_analysis(token)
        derivatives_data = await _data_pipeline.get_derivatives_snapshot(token)
        funding_data = derivatives_data.get("funding_rates") if derivatives_data else None

        alert = await _detector.scan_token(
            token,
            wallet_data=wallet_data,
            derivatives_data=derivatives_data,
            funding_data=funding_data,
            is_refresh=True,
        )

        if alert.total_layers_triggered > 0:
            message = format_refreshed_alert(alert)
        else:
            message = format_no_signals(token_symbol)

        keyboard = get_alert_keyboard(alert.token_symbol, alert.contract_address)
        await query.edit_message_text(message, reply_markup=keyboard)

    except Exception as e:
        log.error("refresh_error", token=token_symbol, error=str(e))
        await query.edit_message_text(f"Refresh error: {str(e)[:200]}")


async def _handle_trade_analysis(query, token_symbol: str) -> None:
    """Handle the trade analysis button. Generate full trade signal."""
    await query.edit_message_text(
        f"Generating trade analysis for ${token_symbol}..."
    )

    try:
        if _data_pipeline is None or _signal_aggregator is None:
            await query.edit_message_text("Signal aggregator not available.")
            return

        token = await _data_pipeline.build_token_profile(token_symbol)
        if token is None:
            await query.edit_message_text(f"Could not find data for ${token_symbol}")
            return

        # Run crime pump detection
        crime_alert = None
        if _detector:
            wallet_data = await _data_pipeline.get_wallet_analysis(token)
            derivatives_data = await _data_pipeline.get_derivatives_snapshot(token)
            funding_data = derivatives_data.get("funding_rates") if derivatives_data else None

            crime_alert = await _detector.scan_token(
                token,
                wallet_data=wallet_data,
                derivatives_data=derivatives_data,
                funding_data=funding_data,
            )
            if crime_alert.total_layers_triggered == 0:
                crime_alert = None

        # Fetch multi-timeframe candle data
        multi_tf_data = {}
        try:
            multi_tf_data = await _data_pipeline.fetch_multi_timeframe_candles(token.symbol)
        except Exception:
            pass

        # Generate trade signal
        trade_signal = await _signal_aggregator.generate_trade_signal(
            token=token,
            crime_pump_alert=crime_alert,
            multi_tf_data=multi_tf_data if multi_tf_data else None,
        )

        message = format_trade_analysis(trade_signal)

        # Add execution mode
        if _execution_engine:
            mode = "Paper Trading" if _execution_engine.paper_trading else "Live Trading"
            message += f"\n\nExecution Mode: {mode}"

        keyboard = get_trade_analysis_keyboard(token_symbol)
        await query.edit_message_text(message, reply_markup=keyboard)

    except Exception as e:
        log.error("trade_analysis_error", token=token_symbol, error=str(e))
        await query.edit_message_text(f"Error: {str(e)[:200]}")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle raw text messages. If the user sends a ticker or contract address
    directly, treat it as a scan request.
    """
    text = update.message.text.strip()

    # Skip if it looks like a command
    if text.startswith("/"):
        return

    # Check if it looks like a ticker (1-10 uppercase chars) or contract address
    clean = text.upper().replace("$", "")

    is_ticker = len(clean) <= 10 and clean.isalnum()
    is_contract = text.startswith("0x") and len(text) >= 40

    if is_ticker or is_contract:
        context.args = [clean]
        await scan_command(update, context)


async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /track command. Start monitoring a position for alerts.
    Usage: /track ZAMA 0.027 0.025 0.035
           /track ZAMA 0.027 0.025 0.035 5x
    Args: ticker entry_price stop_loss take_profit [leverage]
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /track [ticker] [entry] [stop_loss] [take_profit] [leverage]\n"
            "\n"
            "Examples:\n"
            "/track ZAMA 0.027 0.025 0.035\n"
            "/track BTC 67000 65000 72000 5x\n"
            "\n"
            "The bot will monitor and alert you when:\n"
            "  - Take profit is hit\n"
            "  - Stop loss is hit\n"
            "  - Position reaches 50%, 100%, 200% profit\n"
            "  - Position reaches x2, x5, x10\n"
            "  - Liquidation level is reached\n"
            "  - Position drops 25% or 50%"
        )
        return

    if _position_monitor is None:
        await update.message.reply_text("Position monitor not initialized.")
        return

    symbol = context.args[0].upper().replace("$", "")

    try:
        entry_price = float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Invalid entry price.")
        return

    # Parse optional args
    stop_loss = 0.0
    take_profit = 0.0
    leverage = 1

    if len(context.args) >= 3:
        try:
            stop_loss = float(context.args[2])
        except ValueError:
            pass

    if len(context.args) >= 4:
        try:
            take_profit = float(context.args[3])
        except ValueError:
            pass

    if len(context.args) >= 5:
        lev_str = context.args[4].lower().replace("x", "")
        try:
            leverage = int(lev_str)
        except ValueError:
            pass

    # Determine direction from entry vs stop loss
    if stop_loss > 0 and stop_loss < entry_price:
        direction = "LONG"
    elif stop_loss > 0 and stop_loss > entry_price:
        direction = "SHORT"
    else:
        direction = "LONG"  # Default to long

    position = _position_monitor.add_position(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit,
        leverage=leverage,
    )

    # Calculate TP/SL info
    sl_pct = abs(entry_price - stop_loss) / entry_price * 100 if stop_loss > 0 and entry_price > 0 else 0

    lines = [
        f"📌 Position Tracked: ${symbol}",
        "",
        f"Direction: {direction}",
        f"Entry: ${entry_price}",
    ]

    if stop_loss > 0:
        lines.append(f"Stop Loss: ${stop_loss} ({sl_pct:.1f}% away)")
    if take_profit > 0:
        lines.append(f"Take Profit: ${take_profit}")
    if leverage > 1:
        lines.append(f"Leverage: {leverage}x")
        lines.append(f"Liquidation Price: ${position.liquidation_price:.6f}")

    lines.extend([
        "",
        "Active alerts:",
        "  - TP / SL hit notifications",
        "  - 50%, 100%, 200% profit alerts",
        "  - x2, x5, x10 multiplier alerts",
        "  - 25%, 50% loss warnings",
        "  - Liquidation alert",
        "",
        "Use /setalert to add custom price alerts.",
        "Use /close to stop monitoring.",
    ])

    await update.message.reply_text("\n".join(lines))


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /positions command. View all tracked positions.
    Reads from database first (persistent), then syncs to in-memory monitor.
    """
    from utils.database import get_active_positions as db_get_positions

    try:
        # Always query database first (survives Railway redeploys)
        db_positions = await db_get_positions()
    except Exception as e:
        log.error("db_positions_fetch_error", error=str(e))
        db_positions = []

    # Also check in-memory (has live price updates)
    mem_positions = _position_monitor.active_positions if _position_monitor else []

    # Merge: use DB as source of truth, enrich with in-memory live prices
    mem_by_symbol = {p.symbol: p for p in mem_positions}

    if not db_positions and not mem_positions:
        await update.message.reply_text(
            "No active positions being tracked.\n"
            "Use /track [ticker] [entry] [sl] [tp] to start monitoring."
        )
        return

    # Sync DB positions into memory monitor if missing
    if _position_monitor and db_positions:
        for dbp in db_positions:
            sym = dbp.get("symbol", "")
            if sym not in mem_by_symbol:
                _position_monitor.add_position(
                    symbol=sym,
                    direction=dbp.get("direction", "LONG").upper(),
                    entry_price=dbp.get("entry_price", 0),
                    stop_loss=dbp.get("stop_loss", 0),
                    take_profit_1=dbp.get("take_profit_1", 0),
                    take_profit_2=dbp.get("take_profit_2", 0),
                    leverage=dbp.get("leverage", 1),
                    size_usd=dbp.get("size_usd", 0),
                )
                log.info("position_synced_from_db", symbol=sym)
        # Refresh in-memory list
        mem_positions = _position_monitor.active_positions
        mem_by_symbol = {p.symbol: p for p in mem_positions}

    # Build display from DB positions (always complete)
    positions_to_show = []
    for dbp in db_positions:
        sym = dbp.get("symbol", "")
        mem_pos = mem_by_symbol.get(sym)
        positions_to_show.append({
            "symbol": sym,
            "direction": dbp.get("direction", "LONG").upper(),
            "entry_price": dbp.get("entry_price", 0),
            "current_price": mem_pos.current_price if mem_pos else dbp.get("current_price", dbp.get("entry_price", 0)),
            "pnl_pct": mem_pos.pnl_pct if mem_pos else (dbp.get("pnl_pct", 0) or 0),
            "leverage": dbp.get("leverage", 1),
            "stop_loss": dbp.get("stop_loss", 0),
            "take_profit_1": dbp.get("take_profit_1", 0),
            "size_usd": dbp.get("size_usd", 0),
        })

    # Add any in-memory positions not in DB
    db_symbols = {p.get("symbol", "") for p in db_positions}
    for mp in mem_positions:
        if mp.symbol not in db_symbols:
            positions_to_show.append({
                "symbol": mp.symbol,
                "direction": mp.direction,
                "entry_price": mp.entry_price,
                "current_price": mp.current_price,
                "pnl_pct": mp.pnl_pct,
                "leverage": mp.leverage,
                "stop_loss": mp.stop_loss,
                "take_profit_1": mp.take_profit_1,
                "size_usd": mp.size_usd,
            })

    if not positions_to_show:
        await update.message.reply_text(
            "No active positions being tracked.\n"
            "Use /track [ticker] [entry] [sl] [tp] to start monitoring."
        )
        return

    lines = [f"📋 Tracked Positions ({len(positions_to_show)})\n"]

    for pos in positions_to_show:
        entry = pos["entry_price"]
        current = pos["current_price"]
        pnl = pos["pnl_pct"]
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        entry_str = f"${entry:.6f}" if entry < 1 else f"${entry:,.2f}"
        current_str = f"${current:.6f}" if current < 1 else f"${current:,.2f}"

        lines.append(f"{pnl_emoji} ${pos['symbol']} ({pos['direction']})")
        lines.append(f"  Entry: {entry_str}")
        lines.append(f"  Current: {current_str}")
        lines.append(f"  PnL: {pnl:+.1f}%")
        if pos["leverage"] > 1:
            lines.append(f"  Leverage: {pos['leverage']}x")
        if pos["stop_loss"] > 0:
            sl = pos["stop_loss"]
            sl_str = f"${sl:.6f}" if sl < 1 else f"${sl:,.2f}"
            lines.append(f"  SL: {sl_str}")
        if pos["take_profit_1"] > 0:
            tp = pos["take_profit_1"]
            tp_str = f"${tp:.6f}" if tp < 1 else f"${tp:,.2f}"
            lines.append(f"  TP: {tp_str}")
        lines.append("")

    keyboard = get_positions_keyboard()
    await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /pending command. View pending paper limit orders."""
    if not _execution_engine:
        await update.message.reply_text("Execution engine not available.")
        return

    # Check for pending trades awaiting confirmation
    pending_confirms = _execution_engine._pending_trades
    # Check for active limit orders being monitored
    active_limits = getattr(_execution_engine, '_active_limit_orders', {})

    if not pending_confirms and not active_limits:
        await update.message.reply_text(
            "No pending orders.\n\n"
            "Pending orders appear when:\n"
            "- You place a paper limit order (waiting for price to hit)\n"
            "- You start a trade and haven't confirmed yet"
        )
        return

    lines = ["📋 Pending Orders\n"]

    # Show pending trade confirmations
    if pending_confirms:
        lines.append(f"⏳ Awaiting Confirmation ({len(pending_confirms)}):")
        for cid, data in pending_confirms.items():
            signal = data.get("signal")
            created = data.get("created")
            if signal:
                direction = signal.direction.value.upper()
                token = signal.token_symbol
                entry = signal.risk.entry_price
                entry_str = f"${entry:.6f}" if entry < 1 else f"${entry:,.2f}"
                age_s = (datetime.now(timezone.utc) - created).total_seconds() if created else 0
                lines.append(f"  {direction} ${token} at {entry_str}")
                lines.append(f"  ID: {cid} | Age: {age_s:.0f}s")
        lines.append("")

    # Show active limit orders
    if active_limits:
        lines.append(f"📋 Limit Orders Monitoring ({len(active_limits)}):")
        for order_id, order_data in active_limits.items():
            symbol = order_data.get("symbol", "?")
            limit_price = order_data.get("limit_price", 0)
            direction = order_data.get("direction", "?")
            created = order_data.get("created")
            price_str = f"${limit_price:.6f}" if limit_price < 1 else f"${limit_price:,.2f}"
            if created:
                age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                lines.append(f"  {direction} ${symbol} limit at {price_str}")
                lines.append(f"  Waiting: {age_min:.1f} min")
            else:
                lines.append(f"  {direction} ${symbol} limit at {price_str}")
        lines.append("")

    await update.message.reply_text("\n".join(lines))


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /close command. Stop tracking a position."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /close [ticker]\n"
            "Example: /close ZAMA"
        )
        return

    if _position_monitor is None:
        await update.message.reply_text("Position monitor not initialized.")
        return

    symbol = context.args[0].upper().replace("$", "")
    removed = _position_monitor.remove_position(symbol)

    if removed:
        await update.message.reply_text(
            f"✅ ${symbol} position removed from tracking.\n"
            f"Alerts for this position have been stopped."
        )
    else:
        await update.message.reply_text(
            f"No active position found for ${symbol}."
        )


async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /setalert command. Add a custom price alert to a tracked position.
    Usage: /setalert ZAMA 0.05
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setalert [ticker] [target_price]\n"
            "Example: /setalert ZAMA 0.05\n"
            "\n"
            "You will be notified when price reaches this level."
        )
        return

    if _position_monitor is None:
        await update.message.reply_text("Position monitor not initialized.")
        return

    symbol = context.args[0].upper().replace("$", "")
    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid price.")
        return

    added = _position_monitor.add_price_alert(symbol, target_price)
    if added:
        await update.message.reply_text(
            f"🔔 Alert set for ${symbol} at ${target_price}\n"
            f"You will be notified when price reaches this level."
        )
    else:
        await update.message.reply_text(
            f"No active position found for ${symbol}.\n"
            f"Use /track first to start monitoring a position."
        )


async def calc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /calc command. Calculate potential profit/loss for a trade.

    Usage:
        /calc ZAMA 100 5x
        /calc ZAMA 100 5x entry=0.028 tp=0.04 sl=0.026
        /calc ZAMA 50
    """
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Trade Calculator\n"
            "\n"
            "Usage: /calc [ticker] [amount_usd] [leverage]\n"
            "\n"
            "Examples:\n"
            "/calc ZAMA 100 5x\n"
            "/calc BTC 500 10x\n"
            "/calc ZAMA 100 5x entry=0.028 tp=0.04 sl=0.026\n"
            "\n"
            "If no amount given, defaults to $100.\n"
            "If no leverage given, defaults to 1x.\n"
            "You can override entry, tp, sl with custom values.\n"
            "\n"
            "The calculator uses the bot's trade analysis by default, "
            "or your custom values if provided."
        )
        return

    symbol = context.args[0].upper().replace("$", "")

    # Parse amount, leverage, and overrides
    amount_usd = 100.0
    leverage = 1
    custom_entry = None
    custom_tp = None
    custom_sl = None

    for arg in context.args[1:]:
        arg_lower = arg.lower()

        # Check for key=value overrides
        if "=" in arg_lower:
            key, val = arg_lower.split("=", 1)
            try:
                val_f = float(val)
                if key == "entry":
                    custom_entry = val_f
                elif key == "tp":
                    custom_tp = val_f
                elif key == "sl":
                    custom_sl = val_f
            except ValueError:
                pass
            continue

        # Check for leverage (e.g., 5x, 10x)
        if arg_lower.endswith("x"):
            try:
                leverage = int(arg_lower.replace("x", ""))
                continue
            except ValueError:
                pass

        # Otherwise treat as amount
        try:
            amount_usd = float(arg)
        except ValueError:
            pass

    # Get trade analysis from signal aggregator for entry/SL/TP
    entry_price = custom_entry
    stop_loss = custom_sl
    take_profit_1 = custom_tp
    take_profit_2 = None

    # If we don't have custom values, try to get from bot analysis
    if entry_price is None or stop_loss is None or take_profit_1 is None:
        if _signal_aggregator and _data_pipeline:
            try:
                await update.message.reply_text(f"Calculating trade for ${symbol}...")
                token = await _data_pipeline.build_token_profile(symbol)
                if token:
                    multi_tf = {}
                    try:
                        multi_tf = await _data_pipeline.fetch_multi_timeframe_candles(symbol)
                    except Exception:
                        pass
                    signal = await _signal_aggregator.generate_trade_signal(
                        token=token,
                        multi_tf_data=multi_tf if multi_tf else None,
                    )
                    if signal:
                        if entry_price is None:
                            entry_price = signal.risk.entry_price
                        if stop_loss is None:
                            stop_loss = signal.risk.stop_loss
                        if take_profit_1 is None:
                            take_profit_1 = signal.risk.take_profit_1
                        take_profit_2 = signal.risk.take_profit_2
                        # Use signal's leverage if user didn't specify
                        if leverage == 1 and signal.confidence >= 0.4:
                            if signal.confidence >= 0.7:
                                leverage = 10
                            elif signal.confidence >= 0.5:
                                leverage = 5
                            elif signal.confidence >= 0.4:
                                leverage = 3
                            else:
                                leverage = 2
            except Exception as e:
                log.error("calc_analysis_error", error=str(e))

    # Validate we have enough data
    if not entry_price or entry_price <= 0:
        await update.message.reply_text(
            f"Could not get price data for ${symbol}.\n"
            "Try specifying values manually:\n"
            f"/calc {symbol} {amount_usd} {leverage}x entry=0.05 tp=0.08 sl=0.04"
        )
        return

    # Calculate effective position
    effective_position = amount_usd * leverage

    # Determine direction from entry vs SL
    direction = "LONG"
    if stop_loss and stop_loss > entry_price:
        direction = "SHORT"

    # Calculate scenarios
    lines = [
        f"🧮 Trade Calculator: ${symbol}",
        "",
        f"Direction: {direction}",
        f"Amount: ${amount_usd:,.2f}",
        f"Leverage: {leverage}x",
        f"Effective Position: ${effective_position:,.2f}",
    ]

    entry_str = f"${entry_price:.6f}" if entry_price < 1 else f"${entry_price:,.2f}"
    lines.append(f"Entry: {entry_str}")
    lines.append("")

    # TP1 scenario
    if take_profit_1 and take_profit_1 > 0:
        if direction == "LONG":
            tp1_pct = (take_profit_1 - entry_price) / entry_price * 100 * leverage
        else:
            tp1_pct = (entry_price - take_profit_1) / entry_price * 100 * leverage
        tp1_usd = amount_usd * tp1_pct / 100
        tp1_str = f"${take_profit_1:.6f}" if take_profit_1 < 1 else f"${take_profit_1:,.2f}"
        lines.append(f"If TP1 hits ({tp1_str}):")
        lines.append(f"  Profit: +${tp1_usd:,.2f} (+{tp1_pct:.1f}%)")

    # TP2 scenario
    if take_profit_2 and take_profit_2 > 0:
        if direction == "LONG":
            tp2_pct = (take_profit_2 - entry_price) / entry_price * 100 * leverage
        else:
            tp2_pct = (entry_price - take_profit_2) / entry_price * 100 * leverage
        tp2_usd = amount_usd * tp2_pct / 100
        tp2_str = f"${take_profit_2:.6f}" if take_profit_2 < 1 else f"${take_profit_2:,.2f}"
        lines.append(f"If TP2 hits ({tp2_str}):")
        lines.append(f"  Profit: +${tp2_usd:,.2f} (+{tp2_pct:.1f}%)")

    lines.append("")

    # SL scenario
    if stop_loss and stop_loss > 0:
        if direction == "LONG":
            sl_pct = (entry_price - stop_loss) / entry_price * 100 * leverage
        else:
            sl_pct = (stop_loss - entry_price) / entry_price * 100 * leverage
        sl_usd = amount_usd * sl_pct / 100
        sl_str = f"${stop_loss:.6f}" if stop_loss < 1 else f"${stop_loss:,.2f}"
        lines.append(f"If SL hits ({sl_str}):")
        lines.append(f"  Loss: -${sl_usd:,.2f} (-{sl_pct:.1f}%)")

    # Liquidation scenario
    if leverage > 1:
        if direction == "LONG":
            liq_price = entry_price * (1 - 1 / leverage)
        else:
            liq_price = entry_price * (1 + 1 / leverage)
        liq_str = f"${liq_price:.6f}" if liq_price < 1 else f"${liq_price:,.2f}"
        lines.append(f"If liquidated ({liq_str}):")
        lines.append(f"  Loss: -${amount_usd:,.2f} (-100%)")

    # Additional milestones
    lines.append("")
    lines.append("Profit milestones:")
    for mult_pct in [50, 100, 200, 500, 1000]:
        gain = amount_usd * mult_pct / 100
        if direction == "LONG":
            price_at = entry_price * (1 + mult_pct / 100 / leverage)
        else:
            price_at = entry_price * (1 - mult_pct / 100 / leverage)

        if price_at > 0:
            p_str = f"${price_at:.6f}" if price_at < 1 else f"${price_at:,.4f}"
            label = f"x{mult_pct // 100}" if mult_pct >= 100 else f"{mult_pct}%"
            lines.append(f"  {label} profit (+${gain:,.2f}) at {p_str}")

    lines.append("")
    lines.append("Mode: Paper Trading")

    await update.message.reply_text("\n".join(lines))


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /backtest command. Run historical backtesting.

    Usage:
        /backtest BTC 30d          - Test trade analysis signals
        /backtest BTC 30d crime    - Test crime pump detection
        /backtest ETH 90d 10x      - Trade analysis with custom leverage
    """
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Backtest Engine\n"
            "\n"
            "Two modes available:\n"
            "\n"
            "1. Trade Analysis Backtest:\n"
            "/backtest BTC 30d\n"
            "/backtest SOL 7d 10x\n"
            "Tests: Would the bot's trade signals (entry/SL/TP)\n"
            "have been profitable over the last 30 days?\n"
            "\n"
            "2. Crime Pump Detection Backtest:\n"
            "/backtest BTC 30d crime\n"
            "Tests: Would the crime pump detector have\n"
            "triggered on this token? What happened after?\n"
            "\n"
            "Period: 7d, 14d, 30d, 60d, 90d, 180d, 365d\n"
            "Leverage: 1x-20x (default 5x)\n"
        )
        return

    if not _backtester:
        await update.message.reply_text("Backtester not initialized.")
        return

    symbol = context.args[0].upper().replace("$", "")
    days = 30
    leverage = 5
    mode = "trade"  # Default mode

    for arg in context.args[1:]:
        arg_lower = arg.lower()
        if arg_lower in ("crime", "crimepump", "pump", "detection"):
            mode = "crime"
            continue
        if arg_lower.endswith("d"):
            try:
                days = int(arg_lower.replace("d", ""))
            except ValueError:
                pass
        elif arg_lower.endswith("x"):
            try:
                leverage = int(arg_lower.replace("x", ""))
            except ValueError:
                pass

    days = min(max(days, 1), 365)
    leverage = min(max(leverage, 1), 20)

    if mode == "crime":
        await update.message.reply_text(
            f"Running crime pump detection backtest for ${symbol} over {days} days...\n"
            "This may take a moment."
        )
        try:
            from core.backtester import format_crime_backtest_result
            result = await _backtester.run_crime_pump_backtest(
                symbol=symbol, days=days
            )
            output = format_crime_backtest_result(result)
            await update.message.reply_text(output)
        except Exception as e:
            log.error("crime_backtest_error", error=str(e))
            await update.message.reply_text(f"Crime pump backtest failed: {str(e)}")
    else:
        await update.message.reply_text(
            f"Running trade analysis backtest for ${symbol} over {days} days at {leverage}x...\n"
            "This may take a moment."
        )
        try:
            from core.backtester import format_backtest_result
            result = await _backtester.run_backtest(
                symbol=symbol, days=days, leverage=leverage
            )
            output = format_backtest_result(result)
            await update.message.reply_text(output)
        except Exception as e:
            log.error("backtest_error", error=str(e))
            await update.message.reply_text(f"Trade backtest failed: {str(e)}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /stats command. Show performance dashboard."""
    await update.message.reply_text("📊 Calculating performance stats...")

    try:
        stats = await get_performance_stats()

        if stats["total_trades"] == 0:
            # No closed trades yet — show what we have
            active = stats["active_count"]
            await update.message.reply_text(
                "📊 Performance Dashboard\n"
                f"{'=' * 28}\n\n"
                "No closed trades yet.\n\n"
                f"📋 Active Positions: {active}\n\n"
                "Close positions with /close to start\n"
                "building your performance history."
            )
            return

        # Build the dashboard
        total = stats["total_trades"]
        wins = stats["wins"]
        losses = stats["losses"]
        win_rate = stats["win_rate"]
        total_pnl = stats["total_pnl_pct"]
        avg_win = stats["avg_win_pct"]
        avg_loss = stats["avg_loss_pct"]
        pf = stats["profit_factor"]
        avg_lev = stats["avg_leverage"]
        active = stats["active_count"]
        by_dir = stats["by_direction"]

        # Win rate emoji
        if win_rate >= 60:
            wr_emoji = "🟢"
        elif win_rate >= 40:
            wr_emoji = "🟡"
        else:
            wr_emoji = "🔴"

        # PnL emoji
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        # Profit factor rating
        if pf >= 2.0:
            pf_rating = "Excellent"
        elif pf >= 1.5:
            pf_rating = "Good"
        elif pf >= 1.0:
            pf_rating = "Break-even"
        else:
            pf_rating = "Losing"

        lines = [
            "📊 Performance Dashboard",
            f"{'=' * 28}\n",
            f"{wr_emoji} Win Rate: {win_rate:.1f}% ({wins}W / {losses}L)",
            f"{pnl_emoji} Total PnL: {total_pnl:+.1f}%",
            f"📋 Total Trades: {total}",
            f"📋 Active Positions: {active}\n",
            "💰 Trade Averages:",
            f"  Avg Win:  {avg_win:+.1f}%",
            f"  Avg Loss: {avg_loss:+.1f}%",
            f"  Avg Leverage: {avg_lev:.0f}x\n",
            f"⚖️ Profit Factor: {pf:.2f} ({pf_rating})",
        ]

        # Best & worst trade
        best = stats["best_trade"]
        worst = stats["worst_trade"]
        if best:
            lines.append(f"\n🏆 Best Trade: ${best.get('symbol', '?')} "
                        f"({best.get('direction', '?')}) "
                        f"{best.get('pnl_pct', 0):+.1f}%")
        if worst:
            lines.append(f"💀 Worst Trade: ${worst.get('symbol', '?')} "
                        f"({worst.get('direction', '?')}) "
                        f"{worst.get('pnl_pct', 0):+.1f}%")

        # Direction breakdown
        long_total = by_dir["LONG"]["wins"] + by_dir["LONG"]["losses"]
        short_total = by_dir["SHORT"]["wins"] + by_dir["SHORT"]["losses"]
        lines.append(f"\n📈 LONG: {by_dir['LONG']['wins']}W/{by_dir['LONG']['losses']}L"
                    f" ({by_dir['LONG']['wins']/long_total*100:.0f}% WR)" if long_total > 0 else "\n📈 LONG: No trades")
        lines.append(f"📉 SHORT: {by_dir['SHORT']['wins']}W/{by_dir['SHORT']['losses']}L"
                    f" ({by_dir['SHORT']['wins']/short_total*100:.0f}% WR)" if short_total > 0 else "📉 SHORT: No trades")

        # Recent trade history
        recent = stats["closed_trades"]
        if recent:
            lines.append(f"\n📜 Recent Trades (last {len(recent)}):")
            for t in recent[:5]:
                symbol = t.get("symbol", "?")
                direction = t.get("direction", "?").upper()
                pnl = t.get("pnl_pct", 0) or 0
                reason = t.get("close_reason", "Manual")
                emoji = "✅" if pnl > 0 else "❌"
                lines.append(f"  {emoji} ${symbol} ({direction}) {pnl:+.1f}% — {reason}")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        log.error("stats_error", error=str(e))
        await update.message.reply_text(f"Error loading stats: {str(e)[:200]}")
