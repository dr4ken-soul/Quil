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
)
from utils.logger import get_logger
from utils.database import get_active_watchlist, add_to_watchlist, get_recent_alerts

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
        trade_signal = await _signal_aggregator.generate_trade_signal(
            token=token,
            crime_pump_alert=crime_alert if crime_alert.total_layers_triggered > 0 else None,
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
        await add_to_watchlist(ticker, contract_address=contract, reason="User added")
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

    elif data.startswith("watchlist_add:"):
        token_symbol = data.split(":", 1)[1]
        try:
            await add_to_watchlist(token_symbol, reason="Added via button")
            await query.edit_message_text(
                query.message.text + f"\n\n✅ ${token_symbol} added to watchlist",
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

        # Generate trade signal
        trade_signal = await _signal_aggregator.generate_trade_signal(
            token=token,
            crime_pump_alert=crime_alert,
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
    """Handle the /positions command. View all tracked positions."""
    if _position_monitor is None:
        await update.message.reply_text("Position monitor not initialized.")
        return

    active = _position_monitor.active_positions
    if not active:
        await update.message.reply_text(
            "No active positions being tracked.\n"
            "Use /track [ticker] [entry] [sl] [tp] to start monitoring."
        )
        return

    lines = [f"📋 Tracked Positions ({len(active)})\n"]

    for pos in active:
        pnl_emoji = "🟢" if pos.pnl_pct >= 0 else "🔴"
        entry_str = f"${pos.entry_price:.6f}" if pos.entry_price < 1 else f"${pos.entry_price:,.2f}"
        current_str = f"${pos.current_price:.6f}" if pos.current_price < 1 else f"${pos.current_price:,.2f}"

        lines.append(f"{pnl_emoji} ${pos.symbol} ({pos.direction})")
        lines.append(f"  Entry: {entry_str}")
        lines.append(f"  Current: {current_str}")
        lines.append(f"  PnL: {pos.pnl_pct:+.1f}%")
        if pos.leverage > 1:
            lines.append(f"  Leverage: {pos.leverage}x")
        if pos.stop_loss > 0:
            sl_str = f"${pos.stop_loss:.6f}" if pos.stop_loss < 1 else f"${pos.stop_loss:,.2f}"
            lines.append(f"  SL: {sl_str}")
        if pos.take_profit_1 > 0:
            tp_str = f"${pos.take_profit_1:.6f}" if pos.take_profit_1 < 1 else f"${pos.take_profit_1:,.2f}"
            lines.append(f"  TP: {tp_str}")
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
                    signal = await _signal_aggregator.generate_signal(token)
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
