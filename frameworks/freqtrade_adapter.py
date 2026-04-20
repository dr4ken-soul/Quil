"""
Freqtrade Adapter

Adapter interface for integrating Freqtrade's technical analysis strategies
with the Quil signal aggregator. Provides backtesting capability and
indicator-based signal generation.

Note: Requires Freqtrade to be installed separately (pip install freqtrade).
The adapter works as a bridge, not a replacement.
"""

from typing import Optional
from utils.logger import get_logger

log = get_logger("freqtrade_adapter")


class FreqtradeAdapter:
    """
    Bridge between Freqtrade strategies and the Quil signal pipeline.

    Capabilities:
    - Run Freqtrade's technical indicators (EMA, RSI, MACD, Bollinger Bands)
    - Generate buy/sell signals from indicator crossovers
    - Backtest strategies against historical data
    - Provide indicator values to the signal aggregator
    """

    def __init__(self):
        self._available = False
        self._check_availability()

    def _check_availability(self):
        """Check if Freqtrade is installed."""
        try:
            import freqtrade
            self._available = True
            log.info("freqtrade_available", version=getattr(freqtrade, '__version__', 'unknown'))
        except ImportError:
            self._available = False
            log.info("freqtrade_not_installed",
                    message="Install with: pip install freqtrade")

    @property
    def available(self) -> bool:
        return self._available

    async def generate_indicators(self, ohlcv_data) -> dict:
        """
        Generate technical indicators from OHLCV data.

        Even without Freqtrade installed, we compute core indicators
        using pandas-ta as a fallback.
        """
        try:
            import pandas as pd
            import numpy as np

            if ohlcv_data is None or len(ohlcv_data) < 20:
                return {"available": False}

            df = ohlcv_data if isinstance(ohlcv_data, pd.DataFrame) else pd.DataFrame(ohlcv_data)

            indicators = {}

            # EMA crossover
            df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
            df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
            df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

            current = df.iloc[-1]
            indicators["ema_9"] = float(current["ema_9"])
            indicators["ema_21"] = float(current["ema_21"])
            indicators["ema_50"] = float(current["ema_50"])
            indicators["ema_9_above_21"] = current["ema_9"] > current["ema_21"]
            indicators["price_above_ema_50"] = current["close"] > current["ema_50"]

            # RSI
            delta = df["close"].diff()
            gain = delta.clip(lower=0).rolling(window=14).mean()
            loss = (-delta.clip(upper=0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, 1)
            df["rsi"] = 100 - (100 / (1 + rs))
            indicators["rsi"] = float(df["rsi"].iloc[-1])
            indicators["rsi_oversold"] = indicators["rsi"] < 30
            indicators["rsi_overbought"] = indicators["rsi"] > 70

            # MACD
            ema_12 = df["close"].ewm(span=12, adjust=False).mean()
            ema_26 = df["close"].ewm(span=26, adjust=False).mean()
            df["macd"] = ema_12 - ema_26
            df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
            df["macd_histogram"] = df["macd"] - df["macd_signal"]

            indicators["macd"] = float(df["macd"].iloc[-1])
            indicators["macd_signal"] = float(df["macd_signal"].iloc[-1])
            indicators["macd_bullish"] = df["macd"].iloc[-1] > df["macd_signal"].iloc[-1]

            # Bollinger Bands
            df["bb_mid"] = df["close"].rolling(window=20).mean()
            bb_std = df["close"].rolling(window=20).std()
            df["bb_upper"] = df["bb_mid"] + 2 * bb_std
            df["bb_lower"] = df["bb_mid"] - 2 * bb_std

            indicators["bb_upper"] = float(df["bb_upper"].iloc[-1])
            indicators["bb_lower"] = float(df["bb_lower"].iloc[-1])
            indicators["bb_position"] = (
                (current["close"] - df["bb_lower"].iloc[-1]) /
                (df["bb_upper"].iloc[-1] - df["bb_lower"].iloc[-1])
                if (df["bb_upper"].iloc[-1] - df["bb_lower"].iloc[-1]) > 0 else 0.5
            )

            indicators["available"] = True
            return indicators

        except Exception as e:
            log.error("indicator_generation_error", error=str(e))
            return {"available": False, "error": str(e)}

    async def run_backtest(self, symbol: str, timeframe: str = "15m",
                            days: int = 30) -> Optional[dict]:
        """
        Run a backtest using Freqtrade's engine.
        Requires Freqtrade to be installed.
        """
        if not self._available:
            return {
                "available": False,
                "message": "Freqtrade not installed. "
                          "Install with: pip install freqtrade"
            }

        # Freqtrade backtest integration would go here
        # For now return a placeholder
        return {
            "available": True,
            "status": "backtest_not_implemented",
            "message": "Freqtrade backtesting integration is in development."
        }
