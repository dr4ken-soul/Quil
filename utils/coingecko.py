"""
CoinGecko API Helper

Shared utility for all CoinGecko API calls across the bot.
Handles API key authentication, headers, and params automatically.
Used by: backtester, data_pipeline, signal_aggregator, etc.
"""

from config.settings import COINGECKO_API_KEY
from utils.logger import get_logger

log = get_logger("coingecko")

# Base URL for the Demo API
BASE_URL = "https://api.coingecko.com/api/v3"


def get_headers() -> dict:
    """Get headers with API key for CoinGecko requests."""
    headers = {}
    if COINGECKO_API_KEY and COINGECKO_API_KEY != "your_coingecko_key":
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    return headers


def get_params(**extra) -> dict:
    """Get params with API key for CoinGecko requests."""
    params = dict(extra)
    if COINGECKO_API_KEY and COINGECKO_API_KEY != "your_coingecko_key":
        params["x_cg_demo_api_key"] = COINGECKO_API_KEY
    return params


def search_url() -> str:
    """Get the search endpoint URL."""
    return f"{BASE_URL}/search"


def price_url() -> str:
    """Get the simple price endpoint URL."""
    return f"{BASE_URL}/simple/price"


def ohlc_url(coin_id: str) -> str:
    """Get the OHLC endpoint URL for a specific coin."""
    return f"{BASE_URL}/coins/{coin_id}/ohlc"


def market_chart_url(coin_id: str) -> str:
    """Get the market chart endpoint URL for a specific coin."""
    return f"{BASE_URL}/coins/{coin_id}/market_chart"
