"""
Quil Database

Async SQLite database layer for persisting alerts, trade signals, scan results,
and crime pump detection history. All data is logged for backtesting and model refinement.
"""

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional
from utils.logger import get_logger
from config.settings import BASE_DIR

log = get_logger("database")

DB_PATH = BASE_DIR / "quil.db"


async def init_db() -> None:
    """Initialize database tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS crime_pump_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_symbol TEXT NOT NULL,
                contract_address TEXT,
                chain TEXT,
                confidence_level TEXT NOT NULL,
                crime_stage TEXT,
                layers_triggered TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                price REAL,
                market_cap REAL,
                liquidity REAL,
                oi_to_mcap_ratio REAL,
                funding_rate REAL,
                top10_wallet_concentration REAL,
                primary_exchange TEXT,
                recommendation TEXT,
                full_reasoning TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trade_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL NOT NULL,
                timeframe TEXT,
                entry_price REAL,
                stop_loss REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                risk_reward REAL,
                technical_signals TEXT,
                onchain_signals TEXT,
                trap_detection TEXT,
                crime_pump_status TEXT,
                reasoning TEXT,
                framework_source TEXT,
                executed INTEGER DEFAULT 0,
                outcome TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_symbol TEXT NOT NULL,
                contract_address TEXT,
                chain TEXT,
                scan_type TEXT NOT NULL,
                layer1_score REAL DEFAULT 0,
                layer2_score REAL DEFAULT 0,
                layer3_score REAL DEFAULT 0,
                layer4_score REAL DEFAULT 0,
                social_score REAL DEFAULT 0,
                total_score REAL DEFAULT 0,
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS token_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_symbol TEXT NOT NULL,
                contract_address TEXT,
                chain TEXT,
                reason TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_scanned TIMESTAMP,
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS framework_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                framework TEXT NOT NULL,
                token_symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT,
                confidence REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tracked_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL DEFAULT 0,
                stop_loss REAL DEFAULT 0,
                take_profit_1 REAL DEFAULT 0,
                take_profit_2 REAL DEFAULT 0,
                leverage INTEGER DEFAULT 1,
                size_usd REAL DEFAULT 0,
                liquidation_price REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0,
                profit_alerts TEXT,
                multiplier_alerts TEXT,
                custom_price_alerts TEXT,
                fired_alerts TEXT,
                is_open INTEGER DEFAULT 1,
                close_reason TEXT,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_token ON crime_pump_alerts(token_symbol);
            CREATE INDEX IF NOT EXISTS idx_alerts_created ON crime_pump_alerts(created_at);
            CREATE INDEX IF NOT EXISTS idx_signals_token ON trade_signals(token_symbol);
            CREATE INDEX IF NOT EXISTS idx_scan_token ON scan_results(token_symbol);
            CREATE INDEX IF NOT EXISTS idx_watchlist_active ON token_watchlist(active);
            CREATE INDEX IF NOT EXISTS idx_positions_open ON tracked_positions(is_open);
        """)
        await db.commit()
        log.info("database_initialized", path=str(DB_PATH))


async def save_crime_pump_alert(alert_data: dict) -> int:
    """Save a crime pump alert and return its ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO crime_pump_alerts (
                token_symbol, contract_address, chain, confidence_level,
                crime_stage, layers_triggered, signals_json, price,
                market_cap, liquidity, oi_to_mcap_ratio, funding_rate,
                top10_wallet_concentration, primary_exchange,
                recommendation, full_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert_data.get("token_symbol"),
            alert_data.get("contract_address"),
            alert_data.get("chain"),
            alert_data.get("confidence_level"),
            alert_data.get("crime_stage"),
            str(alert_data.get("layers_triggered", [])),
            str(alert_data.get("signals", {})),
            alert_data.get("price"),
            alert_data.get("market_cap"),
            alert_data.get("liquidity"),
            alert_data.get("oi_to_mcap_ratio"),
            alert_data.get("funding_rate"),
            alert_data.get("top10_wallet_concentration"),
            alert_data.get("primary_exchange"),
            alert_data.get("recommendation"),
            alert_data.get("full_reasoning"),
        ))
        await db.commit()
        log.info("alert_saved", token=alert_data.get("token_symbol"), id=cursor.lastrowid)
        return cursor.lastrowid


async def save_trade_signal(signal_data: dict) -> int:
    """Save a trade signal and return its ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO trade_signals (
                token_symbol, direction, confidence, timeframe,
                entry_price, stop_loss, take_profit_1, take_profit_2,
                risk_reward, technical_signals, onchain_signals,
                trap_detection, crime_pump_status, reasoning, framework_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_data.get("token_symbol"),
            signal_data.get("direction"),
            signal_data.get("confidence"),
            signal_data.get("timeframe"),
            signal_data.get("entry_price"),
            signal_data.get("stop_loss"),
            signal_data.get("take_profit_1"),
            signal_data.get("take_profit_2"),
            signal_data.get("risk_reward"),
            str(signal_data.get("technical_signals", [])),
            str(signal_data.get("onchain_signals", [])),
            str(signal_data.get("trap_detection", {})),
            signal_data.get("crime_pump_status"),
            signal_data.get("reasoning"),
            signal_data.get("framework_source"),
        ))
        await db.commit()
        return cursor.lastrowid


async def save_scan_result(scan_data: dict) -> int:
    """Save a scan result and return its ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO scan_results (
                token_symbol, contract_address, chain, scan_type,
                layer1_score, layer2_score, layer3_score, layer4_score,
                social_score, total_score, raw_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_data.get("token_symbol"),
            scan_data.get("contract_address"),
            scan_data.get("chain"),
            scan_data.get("scan_type"),
            scan_data.get("layer1_score", 0),
            scan_data.get("layer2_score", 0),
            scan_data.get("layer3_score", 0),
            scan_data.get("layer4_score", 0),
            scan_data.get("social_score", 0),
            scan_data.get("total_score", 0),
            str(scan_data.get("raw_data", {})),
        ))
        await db.commit()
        return cursor.lastrowid


async def get_latest_alert(token_symbol: str) -> Optional[dict]:
    """Get the most recent alert for a token."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM crime_pump_alerts
            WHERE token_symbol = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (token_symbol,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_recent_alerts(hours: int = 24, limit: int = 50) -> list[dict]:
    """Get recent alerts within a time window."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM crime_pump_alerts
            WHERE created_at >= datetime('now', ? || ' hours')
            ORDER BY created_at DESC
            LIMIT ?
        """, (f"-{hours}", limit))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def add_to_watchlist(token_symbol: str, contract_address: str = None,
                           chain: str = None, reason: str = None) -> dict:
    """Add a token to the monitoring watchlist. Returns status dict."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if token already exists in watchlist
        cursor = await db.execute("""
            SELECT id, active FROM token_watchlist
            WHERE UPPER(token_symbol) = UPPER(?)
        """, (token_symbol,))
        existing = await cursor.fetchone()

        if existing:
            row_id, is_active = existing
            if is_active:
                # Already in watchlist and active
                return {"status": "already_exists", "id": row_id}
            else:
                # Was deactivated, reactivate it
                await db.execute("""
                    UPDATE token_watchlist SET active = 1 WHERE id = ?
                """, (row_id,))
                await db.commit()
                log.info("watchlist_reactivated", token=token_symbol)
                return {"status": "reactivated", "id": row_id}

        # New token — insert
        cursor = await db.execute("""
            INSERT INTO token_watchlist (token_symbol, contract_address, chain, reason)
            VALUES (?, ?, ?, ?)
        """, (token_symbol, contract_address, chain, reason))
        await db.commit()
        log.info("watchlist_added", token=token_symbol)
        return {"status": "added", "id": cursor.lastrowid}


async def get_active_watchlist() -> list[dict]:
    """Get all actively monitored tokens."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM token_watchlist WHERE active = 1
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def save_position(position_data: dict) -> int:
    """Save a tracked position and return its ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO tracked_positions (
                symbol, direction, entry_price, stop_loss,
                take_profit_1, take_profit_2, leverage, size_usd,
                liquidation_price, profit_alerts, multiplier_alerts,
                custom_price_alerts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position_data.get("symbol"),
            position_data.get("direction"),
            position_data.get("entry_price"),
            position_data.get("stop_loss", 0),
            position_data.get("take_profit_1", 0),
            position_data.get("take_profit_2", 0),
            position_data.get("leverage", 1),
            position_data.get("size_usd", 0),
            position_data.get("liquidation_price", 0),
            str(position_data.get("profit_alerts", [])),
            str(position_data.get("multiplier_alerts", [])),
            str(position_data.get("custom_price_alerts", [])),
        ))
        await db.commit()
        log.info("position_saved", symbol=position_data.get("symbol"),
                id=cursor.lastrowid)
        return cursor.lastrowid


async def get_active_positions() -> list[dict]:
    """Get all open tracked positions."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM tracked_positions WHERE is_open = 1
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_position(position_id: int, data: dict) -> None:
    """Update a tracked position."""
    async with aiosqlite.connect(DB_PATH) as db:
        fields = []
        values = []
        for key, value in data.items():
            fields.append(f"{key} = ?")
            values.append(value)
        values.append(position_id)
        await db.execute(
            f"UPDATE tracked_positions SET {', '.join(fields)} WHERE id = ?",
            values
        )
        await db.commit()


async def close_position(position_id: int, reason: str = "Manual") -> None:
    """Close a tracked position."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE tracked_positions
            SET is_open = 0, close_reason = ?, closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (reason, position_id))
        await db.commit()
        log.info("position_closed", id=position_id, reason=reason)
