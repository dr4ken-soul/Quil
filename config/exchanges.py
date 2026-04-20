"""
Exchange-specific configuration

Defines index weights, risk control ratings, and monitoring priorities
for each exchange relevant to crime pump detection.
"""


# Index weight estimates for major perp exchanges
# These represent approximate contribution to mark price calculation
# Higher weight = more influence on liquidation prices across the market
EXCHANGE_INDEX_WEIGHTS = {
    "binance": {
        "weight": 0.35,
        "risk_controls": "strong",
        "position_limits": True,
        "withdrawal_restrictions": True,
        "notes": "Strongest risk controls. Position limits and potential withdrawal freezes constrain manipulation."
    },
    "okx": {
        "weight": 0.20,
        "risk_controls": "moderate",
        "position_limits": True,
        "withdrawal_restrictions": False,
        "notes": "Moderate controls. Less restrictive than Binance but still enforces position limits."
    },
    "bybit": {
        "weight": 0.15,
        "risk_controls": "moderate",
        "position_limits": True,
        "withdrawal_restrictions": False,
        "notes": "Similar to OKX in control strength."
    },
    "bitget": {
        "weight": 0.12,
        "risk_controls": "weak",
        "position_limits": False,
        "withdrawal_restrictions": False,
        "notes": "Weaker risk controls. Thinner order books. Frequently used in crime pump spot operations."
    },
    "gate": {
        "weight": 0.10,
        "risk_controls": "weak",
        "position_limits": False,
        "withdrawal_restrictions": False,
        "notes": "Weak risk controls. Thin order books. Second most common venue for crime pump operations."
    },
    "aster": {
        "weight": 0.05,
        "risk_controls": "minimal",
        "position_limits": False,
        "withdrawal_restrictions": False,
        "notes": "Minimal controls. Does not freeze market maker assets. Preferred for Stage 4 distribution."
    },
    "hyperliquid": {
        "weight": 0.03,
        "risk_controls": "moderate",
        "position_limits": False,
        "withdrawal_restrictions": False,
        "notes": "Decentralized perp exchange. On-chain transparency but no centralized intervention."
    },
}

# Exchanges where crime pump spot operations are most frequently concentrated
CRIME_PUMP_SPOT_EXCHANGES = ["bitget", "gate", "aster"]

# Exchanges to monitor for cold wallet withdrawal activity
COLD_WALLET_MONITOR_EXCHANGES = ["binance", "okx", "bybit", "bitget", "gate"]

# Exchange pairs to monitor for price divergence (manipulation signal)
CROSS_EXCHANGE_PAIRS = [
    ("binance", "bitget"),
    ("binance", "gate"),
    ("binance", "aster"),
    ("bitget", "gate"),
    ("okx", "bitget"),
    ("bybit", "gate"),
]

# Minimum volume threshold (USD) for an exchange to be considered in analysis
MIN_EXCHANGE_VOLUME_USD = 50_000

# Deposit/withdrawal suspension detection
# When an exchange suspends deposits/withdrawals for a token it can isolate that
# market and allow manipulation of perp pricing from cheaper venues
SUSPENSION_CHECK_INTERVAL_SECONDS = 300
