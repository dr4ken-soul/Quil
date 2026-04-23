"""
Quil Database

Supports both SQLite (local/dev) and PostgreSQL (Supabase/Railway production).
Set DATABASE_URL env var to a PostgreSQL connection string to use Supabase.
Without it, defaults to local SQLite.
"""

import os
import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional
from utils.logger import get_logger
from config.settings import BASE_DIR

log = get_logger("database")

# Detect database mode
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

DB_PATH = BASE_DIR / "quil.db"  # SQLite fallback

# PostgreSQL connection pool (lazy-initialized)
_pg_pool = None


async def _get_pg_pool():
    """Get or create the PostgreSQL connection pool."""
    global _pg_pool
    if _pg_pool is None:
        try:
            import asyncpg
            # Fix Supabase URL if needed (some use postgres:// instead of postgresql://)
            url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
            _pg_pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
            log.info("postgres_pool_created")
        except Exception as e:
            log.error("postgres_pool_failed", error=str(e))
            raise
    return _pg_pool


async def init_db() -> None:
    """Initialize database tables."""
    if USE_POSTGRES:
        await _init_postgres()
    else:
        await _init_sqlite()


async def _init_sqlite() -> None:
    """Initialize SQLite tables."""
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
        log.info("database_initialized", backend="sqlite", path=str(DB_PATH))


async def _init_postgres() -> None:
    """Initialize PostgreSQL tables."""
    pool = await _get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crime_pump_alerts (
                id SERIAL PRIMARY KEY,
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
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_signals (
                id SERIAL PRIMARY KEY,
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
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_results (
                id SERIAL PRIMARY KEY,
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
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_watchlist (
                id SERIAL PRIMARY KEY,
                token_symbol TEXT NOT NULL,
                contract_address TEXT,
                chain TEXT,
                reason TEXT,
                added_at TIMESTAMP DEFAULT NOW(),
                last_scanned TIMESTAMP,
                active INTEGER DEFAULT 1
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS framework_signals (
                id SERIAL PRIMARY KEY,
                framework TEXT NOT NULL,
                token_symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_data TEXT,
                confidence REAL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_positions (
                id SERIAL PRIMARY KEY,
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
                opened_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP
            )
        """)
        # Create indexes (PostgreSQL IF NOT EXISTS for indexes)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_alerts_token ON crime_pump_alerts(token_symbol)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_created ON crime_pump_alerts(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_signals_token ON trade_signals(token_symbol)",
            "CREATE INDEX IF NOT EXISTS idx_scan_token ON scan_results(token_symbol)",
            "CREATE INDEX IF NOT EXISTS idx_watchlist_active ON token_watchlist(active)",
            "CREATE INDEX IF NOT EXISTS idx_positions_open ON tracked_positions(is_open)",
        ]:
            await conn.execute(idx_sql)

    log.info("database_initialized", backend="postgresql")


# ── CRUD Operations (dual-backend) ───────────────────────────────────────────

async def save_crime_pump_alert(alert_data: dict) -> int:
    """Save a crime pump alert and return its ID."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO crime_pump_alerts (
                    token_symbol, contract_address, chain, confidence_level,
                    crime_stage, layers_triggered, signals_json, price,
                    market_cap, liquidity, oi_to_mcap_ratio, funding_rate,
                    top10_wallet_concentration, primary_exchange,
                    recommendation, full_reasoning
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                RETURNING id
            """,
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
            )
            log.info("alert_saved", token=alert_data.get("token_symbol"), id=row["id"])
            return row["id"]
    else:
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
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO trade_signals (
                    token_symbol, direction, confidence, timeframe,
                    entry_price, stop_loss, take_profit_1, take_profit_2,
                    risk_reward, technical_signals, onchain_signals,
                    trap_detection, crime_pump_status, reasoning, framework_source
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                RETURNING id
            """,
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
            )
            return row["id"]
    else:
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
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO scan_results (
                    token_symbol, contract_address, chain, scan_type,
                    layer1_score, layer2_score, layer3_score, layer4_score,
                    social_score, total_score, raw_data
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                RETURNING id
            """,
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
            )
            return row["id"]
    else:
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
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM crime_pump_alerts
                WHERE token_symbol = $1
                ORDER BY created_at DESC LIMIT 1
            """, token_symbol)
            return dict(row) if row else None
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM crime_pump_alerts
                WHERE token_symbol = ?
                ORDER BY created_at DESC LIMIT 1
            """, (token_symbol,))
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_recent_alerts(hours: int = 24, limit: int = 50) -> list[dict]:
    """Get recent alerts within a time window."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM crime_pump_alerts
                WHERE created_at >= NOW() - INTERVAL '%s hours'
                ORDER BY created_at DESC LIMIT $1
            """ % hours, limit)
            return [dict(row) for row in rows]
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM crime_pump_alerts
                WHERE created_at >= datetime('now', ? || ' hours')
                ORDER BY created_at DESC LIMIT ?
            """, (f"-{hours}", limit))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def add_to_watchlist(token_symbol: str, contract_address: str = None,
                           chain: str = None, reason: str = None) -> dict:
    """Add a token to the monitoring watchlist. Returns status dict."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, active FROM token_watchlist
                WHERE UPPER(token_symbol) = UPPER($1)
            """, token_symbol)

            if row:
                row_id, is_active = row["id"], row["active"]
                if is_active:
                    return {"status": "already_exists", "id": row_id}
                else:
                    await conn.execute(
                        "UPDATE token_watchlist SET active = 1 WHERE id = $1",
                        row_id
                    )
                    log.info("watchlist_reactivated", token=token_symbol)
                    return {"status": "reactivated", "id": row_id}

            new_row = await conn.fetchrow("""
                INSERT INTO token_watchlist (token_symbol, contract_address, chain, reason)
                VALUES ($1, $2, $3, $4) RETURNING id
            """, token_symbol, contract_address, chain, reason)
            log.info("watchlist_added", token=token_symbol)
            return {"status": "added", "id": new_row["id"]}
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, active FROM token_watchlist
                WHERE UPPER(token_symbol) = UPPER(?)
            """, (token_symbol,))
            existing = await cursor.fetchone()

            if existing:
                row_id, is_active = existing
                if is_active:
                    return {"status": "already_exists", "id": row_id}
                else:
                    await db.execute(
                        "UPDATE token_watchlist SET active = 1 WHERE id = ?",
                        (row_id,)
                    )
                    await db.commit()
                    log.info("watchlist_reactivated", token=token_symbol)
                    return {"status": "reactivated", "id": row_id}

            cursor = await db.execute("""
                INSERT INTO token_watchlist (token_symbol, contract_address, chain, reason)
                VALUES (?, ?, ?, ?)
            """, (token_symbol, contract_address, chain, reason))
            await db.commit()
            log.info("watchlist_added", token=token_symbol)
            return {"status": "added", "id": cursor.lastrowid}


async def get_active_watchlist() -> list[dict]:
    """Get all actively monitored tokens."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM token_watchlist WHERE active = 1"
            )
            return [dict(row) for row in rows]
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM token_watchlist WHERE active = 1"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def save_position(position_data: dict) -> int:
    """Save a tracked position and return its ID."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO tracked_positions (
                    symbol, direction, entry_price, stop_loss,
                    take_profit_1, take_profit_2, leverage, size_usd,
                    liquidation_price, profit_alerts, multiplier_alerts,
                    custom_price_alerts
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                RETURNING id
            """,
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
            )
            log.info("position_saved", symbol=position_data.get("symbol"),
                    id=row["id"])
            return row["id"]
    else:
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
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tracked_positions WHERE is_open = 1"
            )
            return [dict(row) for row in rows]
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_positions WHERE is_open = 1"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_position(position_id: int, data: dict) -> None:
    """Update a tracked position."""
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            fields = []
            values = []
            for i, (key, value) in enumerate(data.items(), 1):
                fields.append(f"{key} = ${i}")
                values.append(value)
            values.append(position_id)
            await conn.execute(
                f"UPDATE tracked_positions SET {', '.join(fields)} WHERE id = ${len(values)}",
                *values
            )
    else:
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
    if USE_POSTGRES:
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE tracked_positions
                SET is_open = 0, close_reason = $1, closed_at = NOW()
                WHERE id = $2
            """, reason, position_id)
            log.info("position_closed", id=position_id, reason=reason)
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE tracked_positions
                SET is_open = 0, close_reason = ?, closed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (reason, position_id))
            await db.commit()
            log.info("position_closed", id=position_id, reason=reason)
