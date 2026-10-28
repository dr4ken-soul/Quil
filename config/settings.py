"""
Quil Configuration

Central settings module. All thresholds, API endpoints, exchange weights,
and detection parameters are defined here and loaded from environment variables.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data_store"
DATA_DIR.mkdir(exist_ok=True)

# ── Telegram ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Blockchain Explorer Keys ──────────────────────────────────────────────────
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY", "")
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
SOLSCAN_API_KEY_2 = os.getenv("SOLSCAN_API_KEY_2", "")
SOLSCAN_API_KEY_3 = os.getenv("SOLSCAN_API_KEY_3", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_API_KEY_2 = os.getenv("HELIUS_API_KEY_2", "")
HELIUS_API_KEY_3 = os.getenv("HELIUS_API_KEY_3", "")
SHYFT_API_KEY = os.getenv("SHYFT_API_KEY", "")
QUICKNODE_SOLANA_RPC_URL = os.getenv("QUICKNODE_SOLANA_RPC_URL", "")
ALCHEMY_SOLANA_API_KEY = os.getenv("ALCHEMY_SOLANA_API_KEY", "")

# ── Market Data Keys ─────────────────────────────────────────────────────────
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")

# ── Exchange API Keys (CCXT) ─────────────────────────────────────────────────
EXCHANGE_CONFIGS = {
    "binance": {
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_SECRET", ""),
        "options": {"defaultType": "future"},
    },
    "bitget": {
        "apiKey": os.getenv("BITGET_API_KEY", ""),
        "secret": os.getenv("BITGET_SECRET", ""),
        "password": os.getenv("BITGET_PASSPHRASE", ""),
    },
    "gate": {
        "apiKey": os.getenv("GATE_API_KEY", ""),
        "secret": os.getenv("GATE_SECRET", ""),
    },
    "bybit": {
        "apiKey": os.getenv("BYBIT_API_KEY", ""),
        "secret": os.getenv("BYBIT_SECRET", ""),
    },
    "okx": {
        "apiKey": os.getenv("OKX_API_KEY", ""),
        "secret": os.getenv("OKX_SECRET", ""),
        "password": os.getenv("OKX_PASSPHRASE", ""),
    },
}

# ── Trading Mode ──────────────────────────────────────────────────────────────
# Set to "live" to enable real trade execution. Default is "paper".
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()
PAPER_TRADING = TRADING_MODE != "live"

# ── Portfolio Budget ──────────────────────────────────────────────────────────
# Total USD budget for position sizing. Each trade uses a % of this.
# Set via PORTFOLIO_VALUE env var in Railway. Default $1000.
PORTFOLIO_VALUE = float(os.getenv("PORTFOLIO_VALUE", "1000.0"))

# ── Social ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'quil.db'}")

# ── Scanner Settings ─────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "15"))
MIN_CONFIDENCE_ALERT = float(os.getenv("MIN_CONFIDENCE_ALERT", "0.5"))
MIN_LAYERS_CONFIRMED = int(os.getenv("MIN_LAYERS_CONFIRMED", "2"))
AUTONOMOUS_WATCH_ALERTS = os.getenv("AUTONOMOUS_WATCH_ALERTS", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# ── Detection Thresholds ─────────────────────────────────────────────────────

# Layer 1: On-Chain Supply Control
SUPPLY_CONCENTRATION_THRESHOLD = 0.90       # 90% supply in few wallets
ACCUMULATION_WINDOW_HOURS = 72              # 24-72h pre-pump accumulation window
COLD_WALLET_WITHDRAWAL_ALERT_USD = 500_000  # Flag withdrawals above this
TOKEN_AGE_SUSPICIOUS_DAYS = 30              # Tokens under 30 days old get extra scrutiny
TRANSFER_SPIKE_MULTIPLIER = 5.0             # 5x normal transfer volume

# Layer 2: Perp / Index Manipulation
OI_TO_MCAP_HIGH_RATIO = 0.5                # OI > 50% of mcap is suspicious
VOLUME_TO_MCAP_SPIKE_RATIO = 2.0           # Volume > 200% of mcap in hours before pump
ORDER_BOOK_THIN_THRESHOLD = 0.02           # Less than 2% depth relative to mcap
PRICE_SUPPRESSION_THEN_SPIKE_PCT = 15.0    # 15% move after flat period

# Layer 3: Funding Rate Dynamics
FUNDING_RATE_ALERT_THRESHOLD = -0.015      # -1.5% funding rate triggers alert
FUNDING_RATE_CRITICAL_THRESHOLD = -0.02    # -2% funding rate is critical
FUNDING_CHECK_INTERVAL_HOURS = 4           # Standard 4h funding period

# Layer 4: Spot / Order Book Manipulation
SPOOF_ORDER_DETECTION_WINDOW_SEC = 60      # Orders appearing/disappearing within 60s
LIQUIDATION_CASCADE_THRESHOLD = 3          # 3+ consecutive liquidations in same direction
CROSS_EXCHANGE_PRICE_DIVERGENCE_PCT = 2.0  # 2% price diff between exchanges

# ── Lifecycle Stage Thresholds ───────────────────────────────────────────────
STAGE1_OI_RISE_PCT = 20.0                  # OI up 20%+ with price rising
STAGE3_OI_DROP_WITH_VOLUME_SPIKE = True    # OI drops + volume spikes = trap pattern
STAGE4_FUNDING_DISTRIBUTION_LEVEL = -0.02  # Funding at -2% during distribution

# ── Crime Pump Priority Exchanges ────────────────────────────────────────────
# Exchanges with historically weak risk controls (higher manipulation likelihood)
CRIME_PUMP_PRIORITY_EXCHANGES = ["bitget", "gate"]
# Index weight significance threshold
INDEX_WEIGHT_SIGNIFICANCE = 0.25           # Exchange with >25% index weight is significant

# ── hl-trap-bot Settings ─────────────────────────────────────────────────────
TRAP_BOT_LOOKBACK_BARS = 50
TRAP_BOT_EMA_FAST = 9
TRAP_BOT_EMA_SLOW = 21
TRAP_BOT_ATR_PERIOD = 14
TRAP_BOT_VOLUME_SPIKE_MULTIPLIER = 2.0
TRAP_BOT_EXHAUSTION_CANDLE_MULTIPLIER = 2.5
TRAP_BOT_CONFIDENCE_THRESHOLD = 0.65

# ── Risk Management ─────────────────────────────────────────────────────────
MAX_POSITION_SIZE_PCT = 5.0                # Max 5% of portfolio per position
MAX_CORRELATED_POSITIONS = 3               # Max 3 correlated positions
STOP_LOSS_BASE_PCT = 3.0                   # Base stop loss percentage
STOP_LOSS_VOLATILITY_MULTIPLIER = 1.5      # Tighten stop by this * ATR in high vol
MAX_DAILY_LOSS_PCT = 10.0                  # Circuit breaker at 10% daily loss

# ── DexScreener ──────────────────────────────────────────────────────────────
DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
DEXSCREENER_RATE_LIMIT = 300               # Requests per minute

# ── Coinglass ────────────────────────────────────────────────────────────────
COINGLASS_BASE_URL = "https://open-api-v4.coinglass.com"

# ── Supported Chains ─────────────────────────────────────────────────────────
SUPPORTED_CHAINS = {
    "ethereum": {
        "explorer_url": "https://api.etherscan.io/v2/api",
        "api_key_env": "ETHERSCAN_API_KEY",
        "chain_id": 1,
    },
    "bsc": {
        "explorer_url": "https://api.etherscan.io/v2/api",
        "api_key_env": "ETHERSCAN_API_KEY",
        "legacy_api_key_env": "BSCSCAN_API_KEY",
        "chain_id": 56,
    },
    "solana": {
        "explorer_url": "https://api.solscan.io",
        "api_key_env": "SOLSCAN_API_KEY",
        "chain_id": None,
    },
}

# ── Monitored Crime Coin Case Studies ────────────────────────────────────────
HISTORICAL_CRIME_COINS = [
    "RAVE", "STO", "ORDI", "IP", "SIREN", "MYX", "COAI", "AIA"
]
