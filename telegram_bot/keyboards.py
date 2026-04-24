"""
Telegram Inline Keyboards

Provides inline keyboard layouts for the Telegram bot interface,
including the refresh button that appears on every analysis output.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def get_alert_keyboard(token_symbol: str, contract_address: str = None) -> InlineKeyboardMarkup:
    """
    Build the inline keyboard for a crime pump alert.
    Includes refresh button and additional action buttons.
    """
    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Refresh Analysis",
                callback_data=f"refresh:{token_symbol}"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Full Trade Analysis",
                callback_data=f"trade_analysis:{token_symbol}"
            ),
            InlineKeyboardButton(
                "📋 Add to Watchlist",
                callback_data=f"watchlist_add:{token_symbol}"
            ),
        ],
    ]

    if contract_address:
        buttons.append([
            InlineKeyboardButton(
                "🔗 DexScreener",
                url=f"https://dexscreener.com/search?q={contract_address}"
            ),
        ])

    return InlineKeyboardMarkup(buttons)


def get_trade_analysis_keyboard(token_symbol: str) -> InlineKeyboardMarkup:
    """Build keyboard for trade analysis output."""
    buttons = [
        [
            InlineKeyboardButton(
                "✅ Execute Trade",
                callback_data=f"execute_trade:{token_symbol}"
            ),
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"refresh_trade:{token_symbol}"
            ),
        ],
        [
            InlineKeyboardButton(
                "🚨 Crime Pump Check",
                callback_data=f"crime_check:{token_symbol}"
            ),
            InlineKeyboardButton(
                "📋 Watchlist",
                callback_data=f"watchlist_add:{token_symbol}"
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_scan_summary_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for scan summary."""
    buttons = [
        [
            InlineKeyboardButton(
                "📡 Force Rescan",
                callback_data="force_rescan"
            ),
            InlineKeyboardButton(
                "📋 View Watchlist",
                callback_data="view_watchlist"
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_watchlist_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for watchlist view."""
    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Refresh All",
                callback_data="refresh_watchlist"
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_positions_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for /positions view with refresh button."""
    buttons = [
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh_positions"
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)
