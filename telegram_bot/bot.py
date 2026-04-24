"""
Telegram Bot Setup

Main bot initialization, command registration, and autonomous alert sending.
Uses python-telegram-bot to interact with the Telegram Bot API.
"""

import asyncio
from telegram import Bot
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from telegram_bot.handlers import (
    start_command,
    help_command,
    scan_command,
    trade_command,
    risk_command,
    track_command,
    positions_command,
    close_command,
    setalert_command,
    calc_command,
    backtest_command,
    sell_command,
    watch_command,
    watchlist_command,
    alerts_command,
    status_command,
    stats_command,
    handle_callback,
    handle_text_message,
    set_detector,
    set_data_pipeline,
    set_signal_aggregator,
    set_execution_engine,
    set_position_monitor,
    set_backtester,
)
from telegram_bot.formatters import format_crime_pump_alert
from telegram_bot.keyboards import get_alert_keyboard
from models.alert import CrimePumpAlert
from utils.logger import get_logger
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = get_logger("telegram_bot")


class QuilTelegramBot:
    """
    Telegram bot interface for the Quil crime pump detection engine.

    Handles command registration, callback queries, and autonomous alert sending.
    Every analysis output includes a refresh button.
    """

    def __init__(self, detector=None, data_pipeline=None,
                 signal_aggregator=None, execution_engine=None,
                 position_monitor=None, backtester=None):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.app: Application = None
        self.bot: Bot = None
        self._detector = detector
        self._data_pipeline = data_pipeline
        self._signal_aggregator = signal_aggregator
        self._execution_engine = execution_engine
        self._position_monitor = position_monitor
        self._backtester = backtester

    async def initialize(self) -> None:
        """Build and configure the Telegram bot application."""
        if not self.token:
            log.error("telegram_no_token",
                     message="TELEGRAM_BOT_TOKEN not set. Bot cannot start.")
            return

        # Inject dependencies into handlers
        if self._detector:
            set_detector(self._detector)
        if self._data_pipeline:
            set_data_pipeline(self._data_pipeline)
        if self._signal_aggregator:
            set_signal_aggregator(self._signal_aggregator)
        if self._execution_engine:
            set_execution_engine(self._execution_engine)
        if self._position_monitor:
            set_position_monitor(self._position_monitor)
        if self._backtester:
            set_backtester(self._backtester)

        # Build the application
        self.app = Application.builder().token(self.token).build()
        self.bot = self.app.bot

        # Register command handlers
        self.app.add_handler(CommandHandler("start", start_command))
        self.app.add_handler(CommandHandler("help", help_command))
        self.app.add_handler(CommandHandler("scan", scan_command))
        self.app.add_handler(CommandHandler("trade", trade_command))
        self.app.add_handler(CommandHandler("analyze", trade_command))
        self.app.add_handler(CommandHandler("risk", risk_command))
        self.app.add_handler(CommandHandler("track", track_command))
        self.app.add_handler(CommandHandler("positions", positions_command))
        self.app.add_handler(CommandHandler("close", close_command))
        self.app.add_handler(CommandHandler("setalert", setalert_command))
        self.app.add_handler(CommandHandler("calc", calc_command))
        self.app.add_handler(CommandHandler("backtest", backtest_command))
        self.app.add_handler(CommandHandler("watch", watch_command))
        self.app.add_handler(CommandHandler("watchlist", watchlist_command))
        self.app.add_handler(CommandHandler("alerts", alerts_command))
        self.app.add_handler(CommandHandler("status", status_command))
        self.app.add_handler(CommandHandler("sell", sell_command))
        self.app.add_handler(CommandHandler("stats", stats_command))

        # Register callback handler for inline buttons (refresh, etc.)
        self.app.add_handler(CallbackQueryHandler(handle_callback))

        # Register message handler for raw text (ticker/contract auto-scan)
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_message,
        ))

        log.info("telegram_bot_initialized")

    async def start_polling(self) -> None:
        """Start the bot in polling mode."""
        if self.app is None:
            await self.initialize()

        if self.app:
            log.info("telegram_bot_starting")
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            log.info("telegram_bot_stopped")

    async def send_alert(self, alert: CrimePumpAlert) -> None:
        """
        Send a crime pump alert to the configured Telegram chat.
        Used by the autonomous monitoring system.
        """
        if not self.bot or not self.chat_id:
            log.warning("cannot_send_alert", reason="Bot or chat_id not configured")
            return

        try:
            message = format_crime_pump_alert(alert)
            keyboard = get_alert_keyboard(alert.token_symbol, alert.contract_address)

            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=keyboard,
            )

            log.info("alert_sent", token=alert.token_symbol,
                    confidence=alert.confidence_level.value)

        except Exception as e:
            log.error("alert_send_error", token=alert.token_symbol, error=str(e))

    async def send_message(self, text: str) -> None:
        """Send a plain text message to the configured chat."""
        if not self.bot or not self.chat_id:
            return

        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception as e:
            log.error("message_send_error", error=str(e))
