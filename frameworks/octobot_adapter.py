"""
OctoBot Adapter

Adapter interface for integrating OctoBot's AI-driven trading strategies
with the Quil signal pipeline. Provides grid trading, DCA, and AI-based
signal generation.

Note: Requires OctoBot to be installed separately.
"""

from typing import Optional
from utils.logger import get_logger

log = get_logger("octobot_adapter")


class OctoBotAdapter:
    """
    Bridge between OctoBot strategies and the Quil signal pipeline.

    Capabilities:
    - Grid trading parameter generation
    - DCA (Dollar Cost Averaging) strategy signals
    - AI-based pattern recognition
    - Evaluator/Strategy pipeline integration
    """

    def __init__(self):
        self._available = False
        self._check_availability()

    def _check_availability(self):
        """Check if OctoBot is installed."""
        try:
            import octobot
            self._available = True
        except ImportError:
            self._available = False
            log.info("octobot_not_installed",
                    message="Install separately from octobot.cloud")

    @property
    def available(self) -> bool:
        return self._available

    async def generate_grid_parameters(self, symbol: str, price: float,
                                        volatility: float) -> dict:
        """
        Generate grid trading parameters based on current price and volatility.

        Returns grid bounds, number of levels, and order sizes.
        """
        if price <= 0 or volatility <= 0:
            return {"available": False}

        # Simple grid calculation even without OctoBot
        grid_range_pct = volatility * 3  # Grid spans 3x the volatility
        upper = price * (1 + grid_range_pct / 100)
        lower = price * (1 - grid_range_pct / 100)
        levels = 10  # Default grid levels

        level_spacing = (upper - lower) / levels

        return {
            "available": True,
            "upper_bound": round(upper, 8),
            "lower_bound": round(lower, 8),
            "levels": levels,
            "level_spacing": round(level_spacing, 8),
            "grid_range_pct": round(grid_range_pct, 1),
            "source": "quil_builtin",
        }

    async def generate_dca_plan(self, symbol: str, price: float,
                                 total_investment: float,
                                 num_entries: int = 5) -> dict:
        """
        Generate a DCA (Dollar Cost Averaging) entry plan.

        Splits total investment across multiple price levels for
        averaging into a position.
        """
        if price <= 0 or total_investment <= 0:
            return {"available": False}

        per_entry = total_investment / num_entries

        # DCA levels spaced 3% apart below current price
        levels = []
        for i in range(num_entries):
            level_price = price * (1 - 0.03 * i)
            levels.append({
                "level": i + 1,
                "price": round(level_price, 8),
                "amount_usd": round(per_entry, 2),
                "discount_pct": round(3 * i, 1),
            })

        avg_entry = sum(l["price"] for l in levels) / len(levels)

        return {
            "available": True,
            "symbol": symbol,
            "total_investment": total_investment,
            "num_entries": num_entries,
            "levels": levels,
            "average_entry": round(avg_entry, 8),
            "source": "quil_builtin",
        }
