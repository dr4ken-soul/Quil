"""
Quil: Crypto Trading Bot with Crime Pump Detection Engine

Main entry point. Initializes all systems, starts the autonomous scanner,
and runs the Telegram bot interface.
"""

import asyncio
import signal
import sys
from datetime import datetime, timedelta, timezone

from config.settings import SCAN_INTERVAL_SECONDS, ALERT_COOLDOWN_MINUTES, PAPER_TRADING, PORTFOLIO_VALUE
from core.data_pipeline import DataPipeline
from core.signal_aggregator import SignalAggregator
from core.execution_engine import ExecutionEngine
from core.risk_manager import RiskManager
from core.position_monitor import PositionMonitor
from core.backtester import BacktestEngine
from detection.crime_pump_detector import CrimePumpDetector
from telegram_bot.bot import QuilTelegramBot
from utils.logger import setup_logging, get_logger
from utils.database import init_db
from models.token import Token, TokenMetrics

log = get_logger("main")


def utcnow():
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class QuilBot:
    """
    Main application orchestrator.

    Manages the lifecycle of:
    - Data pipeline (all data sources)
    - Crime pump detection engine (4 detection layers + lifecycle tracker)
    - Autonomous market scanner
    - Telegram bot interface
    - Execution engine (paper or live trading)
    """

    def __init__(self):
        self.data_pipeline = DataPipeline()
        self.detector = CrimePumpDetector()
        self.signal_aggregator = SignalAggregator()
        self.risk_manager = RiskManager(portfolio_value_usd=PORTFOLIO_VALUE)
        self.position_monitor = PositionMonitor(
            data_pipeline=self.data_pipeline,
        )
        self.execution_engine = ExecutionEngine(
            risk_manager=self.risk_manager,
            paper_trading=PAPER_TRADING,
            position_monitor=self.position_monitor,
        )
        self.backtester = BacktestEngine(
            signal_aggregator=self.signal_aggregator,
            detector=self.detector,
        )
        self.telegram = QuilTelegramBot(
            detector=self.detector,
            data_pipeline=self.data_pipeline,
            signal_aggregator=self.signal_aggregator,
            execution_engine=self.execution_engine,
            position_monitor=self.position_monitor,
            backtester=self.backtester,
        )
        self._running = False
        self._scan_task: asyncio.Task = None
        self._monitor_task: asyncio.Task = None
        self._stop_event = asyncio.Event()
        # Track cooldowns per token
        self._alert_cooldowns: dict[str, datetime] = {}

    async def start(self) -> None:
        """Start all systems and keep running until stopped."""
        log.info("quil_starting", version="1.0.0")

        # Initialize database
        await init_db()
        log.info("database_ready")

        # Initialize exchange connections (if live trading mode)
        if not PAPER_TRADING:
            exchange_results = await self.execution_engine.initialize_exchanges()
            connected = [k for k, v in exchange_results.items() if v]
            if connected:
                log.info("exchanges_connected", exchanges=connected)
            else:
                log.warning("no_exchanges_connected",
                           note="Live mode active but no exchanges connected. "
                                "Add API keys to enable trading.")

        # Initialize Telegram bot
        await self.telegram.initialize()

        # Start the autonomous scanner
        self._running = True
        self._scan_task = asyncio.create_task(self._autonomous_scan_loop())
        log.info("autonomous_scanner_started",
                interval=f"{SCAN_INTERVAL_SECONDS}s")

        # Start position monitor (checks prices every 5 seconds)
        self.position_monitor._telegram = self.telegram
        self._monitor_task = asyncio.create_task(
            self.position_monitor.start_monitoring()
        )
        log.info("position_monitor_started")

        # Send startup message
        mode = "📝 Paper Trading" if PAPER_TRADING else "🔴 LIVE Trading"
        await self.telegram.send_message(
            f"🟢 Quil Crime Pump Detection Engine is online.\n"
            f"Mode: {mode}\n"
            f"Budget: ${PORTFOLIO_VALUE:,.0f}\n"
            f"Autonomous scanning active. Send /help for commands."
        )

        # Start Telegram polling
        await self.telegram.start_polling()
        log.info("quil_running", message="Bot is running. Press Ctrl+C to stop.")

        # Keep running until stop event is set
        await self._stop_event.wait()

    async def stop(self) -> None:
        """Gracefully shut down all systems."""
        log.info("quil_stopping")
        self._running = False
        self._stop_event.set()

        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass

        try:
            await self.telegram.stop()
        except Exception as e:
            log.error("telegram_stop_error", error=str(e))

        await self.data_pipeline.close()
        log.info("quil_stopped")

    async def _autonomous_scan_loop(self) -> None:
        """
        Continuous market scanning loop.

        Scans DexScreener for volume anomalies, builds token profiles
        for candidates, runs the crime pump detection pipeline, and
        sends alerts via Telegram when patterns are detected.
        """
        log.info("scan_loop_started")

        # Wait for bot to fully initialize
        await asyncio.sleep(10)

        while self._running:
            try:
                scan_start = utcnow()
                log.info("scan_cycle_start")

                # Step 1: Find tokens with volume anomalies
                anomalies = await self.data_pipeline.scan_for_anomalies()
                log.info("anomalies_found", count=len(anomalies))

                alerts_sent = 0
                watch_count = 0

                # Step 2: Analyze each anomaly candidate
                for anomaly in anomalies[:20]:  # Limit per cycle
                    try:
                        symbol = anomaly.get("symbol", "UNKNOWN")

                        # Check cooldown
                        if self._is_on_cooldown(symbol):
                            continue

                        # Build full token profile
                        token = await self.data_pipeline.build_token_profile(
                            anomaly.get("address", symbol)
                        )

                        if token is None:
                            continue

                        # Pre-populate with anomaly data we already have
                        token.metrics.volume_1h = anomaly.get("volume_1h", token.metrics.volume_1h)
                        token.metrics.volume_5m = anomaly.get("volume_5m", token.metrics.volume_5m)

                        # Get additional data
                        wallet_data = await self.data_pipeline.get_wallet_analysis(token)
                        derivatives_data = await self.data_pipeline.get_derivatives_snapshot(token)
                        funding_data = derivatives_data.get("funding_rates") if derivatives_data else None

                        # Run crime pump detection
                        alert = await self.detector.scan_token(
                            token,
                            wallet_data=wallet_data,
                            derivatives_data=derivatives_data,
                            funding_data=funding_data,
                        )

                        # Send alert if warranted
                        if self.detector.should_send_alert(alert):
                            if alert.is_confirmed:
                                await self.telegram.send_alert(alert)
                                alerts_sent += 1
                                self._set_cooldown(symbol)
                            else:
                                watch_count += 1

                        # Small delay between tokens to respect rate limits
                        await asyncio.sleep(2)

                    except Exception as e:
                        log.error("token_scan_error",
                                token=anomaly.get("symbol", "?"),
                                error=str(e))
                        continue

                scan_duration = (utcnow() - scan_start).total_seconds()
                log.info("scan_cycle_complete",
                        duration=f"{scan_duration:.1f}s",
                        anomalies=len(anomalies),
                        alerts_sent=alerts_sent,
                        watch_count=watch_count)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("scan_loop_error", error=str(e))

            # Wait for next scan cycle
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if a token is on alert cooldown."""
        if symbol in self._alert_cooldowns:
            cooldown_until = self._alert_cooldowns[symbol]
            if utcnow() < cooldown_until:
                return True
        return False

    def _set_cooldown(self, symbol: str) -> None:
        """Set alert cooldown for a token."""
        self._alert_cooldowns[symbol] = (
            utcnow() + timedelta(minutes=ALERT_COOLDOWN_MINUTES)
        )


async def main():
    """Application entry point."""
    setup_logging("INFO")
    log.info("quil_initializing")

    bot = QuilBot()

    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("keyboard_interrupt")
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nQuil stopped.")

