# Quil - Crime Pump Detection & Trading Bot

Autonomous crypto trading bot with a four-layer crime pump detection system, expert-level technical analysis, dual-mode backtesting, real-time position monitoring, and live/paper trade execution via Bybit. Features Supabase PostgreSQL for persistent data, Bybit real-time price feeds, 8 candlestick pattern recognition, and support/resistance detection.

## Build Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Foundation (config, models, database, logging) | ✅ Complete |
| 2 | Data layer (Bybit, CoinGecko, DexScreener, Coinglass, explorers) | ✅ Complete |
| 3 | Crime pump detection (4 layers + lifecycle tracker) | ✅ Complete |
| 4 | hl-trap-bot 5-gate pipeline (used by trade analysis when candles are available) | ✅ Complete |
| 5 | Framework adapters (Freqtrade indicators, public cross-exchange data, OctoBot context) | ✅ Complete |
| 6 | Signal aggregation + execution engine + risk manager | ✅ Complete |
| 7 | Telegram bot interface (all commands) | ✅ Complete |
| 8 | Autonomous market scanning | ✅ Complete |
| 9 | Position monitoring & alerts (TP/SL/liquidation/custom) | ✅ Complete |
| 10 | Backtesting engine (Trade + Crime Pump modes) | ✅ Complete |
| 10.5 | Expert chart analysis + Bybit price pipeline + bug fixes | ✅ Complete |
| 11 | Live execution (Execute Trade button + /sell + paper/live mode) | ✅ Complete |
| 11.5 | Supabase PostgreSQL + position tracking fix | ✅ Complete |
| 12 | Performance dashboard (/stats) | ✅ Complete |
| — | **Deployed on Railway (24/7)** | ✅ Live |
| 13 | Smart limit orders (optimal entry) | ✅ Complete |

## Three Main Features

### 1. Crime Pump Detection
Scans the market continuously for coordinated manipulation across 4 detection layers (on-chain supply, perp/index manipulation, funding rate dynamics, order book manipulation). Classifies tokens into lifecycle stages (accumulation → OI matrix → trap → distribution).

The autonomous scanner processes up to 20 watchlist entries and 20 new anomaly candidates per cycle. Watchlist entries are prioritized by oldest scan time. Watch and confirmed alerts are sent through Telegram subject to the configured cooldown.

### 2. Trading Analysis
Expert-level technical analysis engine that reads charts independently. Uses RSI, EMA Crossovers, MACD, Bollinger Bands, 8 candlestick patterns, and support/resistance levels to generate trade signals with direction (Long/Short), entry, SL, TP1, TP2, leverage, and position sizing. Crime pump detection is a **warning overlay only** — it does not drive trade direction.

### 3. Trade Execution
Dual-mode trade execution system:
- **Paper Mode (default):** Simulated trades — logs signals, tracks positions with live PnL, sends TP/SL alerts. No real money involved.
- **Live Mode:** Places real market orders on Bybit via CCXT with automatic leverage setting, conditional SL/TP orders, and position tracking. Requires a two-step confirmation (Execute → Confirm) to prevent accidental trades.

## Price Pipeline

```
Token Price Resolution (priority order):
1. Bybit Public API    → Real-time exchange price (no key needed)
2. CoinGecko Search    → Dynamic symbol search (works for ANY token)
3. DexScreener         → DEX pool price (fallback for DEX-only tokens)
```

## Data Sources and Coverage

- DexScreener pair prices, volume, liquidity, and venue names are used for DEX market context.
- CCXT public spot endpoints collect Binance, Bybit, and OKX prices and a Bybit order book when that symbol is listed. This adapter reads market data only; it does not place orders.
- CoinGlass API v4 supplies current and historical open interest, funding, global and top-trader long/short ratios, liquidations, and liquidation heatmaps. Coverage depends on the token, endpoint, and CoinGlass plan.
- Etherscan/BscScan holder and transfer endpoints can populate holder concentration, contract age, and transfer activity when those endpoints and API keys are available. Solscan holder coverage is also best-effort.
- Twitter/X mention metrics are optional and require `TWITTER_BEARER_TOKEN`. Quil does not have a Telegram message-search provider; a missing Telegram count is treated as unknown.

Unknown values stay unknown. A missing holder list does not mean that holders are concentrated, an unavailable liquidity-lock source does not mean liquidity is unlocked, and missing social data does not count as a quiet market. Cold-wallet outflows are not available without a provider that identifies exchange-controlled wallets.

Crime-pump alerts require at least `MIN_LAYERS_CONFIRMED` triggered layers and at least that many evaluable layers. A single-layer result is a watch alert. The crime-pump backtest replays the same four analyzers across historical candles; the candle dataset does not include historical holder balances, funding, or order-book snapshots, so those checks are reported as unevaluable.

## Telegram Commands

| Command | What It Does |
|---------|-------------|
| `/scan [ticker]` | Crime pump analysis (4 detection layers) |
| `/trade [ticker]` | Full trade analysis with entry/SL/TP/leverage + Execute button |
| `/analyze [ticker]` | Alias for /trade |
| `/calc [ticker] [amount] [leverage]` | Calculate profit/loss for a trade setup |
| `/backtest [ticker] [period]` | Trade analysis backtesting |
| `/backtest [ticker] [period] crime` | Crime pump detection backtesting |
| `/track [ticker] [entry] [sl] [tp] [leverage]` | Monitor a position for alerts |
| `/positions` | View all tracked positions with live PnL |
| `/close [ticker]` | Stop tracking a position |
| `/sell [ticker]` | Close a live position on the exchange |
| `/setalert [ticker] [price]` | Custom price alert |
| `/watch [ticker]` | Add to continuous monitoring (no duplicates) |
| `/watchlist` | View monitored tokens |
| `/alerts` | View recent detections |
| `/risk` | Portfolio risk status and circuit breaker |
| `/status` | Data source health check |
| `/stats` | Performance dashboard (win rate, PnL, trade history) |

### Trade Execution Flow
1. `/trade ZAMA` → Get analysis with entry, SL, TP, leverage
2. Click **"✅ Execute Trade"** → See confirmation details
3. Click **"✅ Confirm & Execute"** → Trade executed (paper or live)
4. `/positions` → Track position with live PnL updates
5. `/sell ZAMA` → Close position (live mode) or `/close ZAMA` (stop tracking)

## Setup

1. Copy `.env.example` to `.env` and fill in your API keys
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the bot:

```bash
python main.py
```

## Environment Variables

### Required
| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot authentication |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |

### Database (Recommended)
| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Supabase PostgreSQL connection string (persistent data) |

### Live Trading (Optional)
| Variable | Purpose |
|----------|---------|
| `TRADING_MODE` | `paper` (default) or `live` |
| `BYBIT_API_KEY` | Bybit API key (required for live trading) |
| `BYBIT_SECRET` | Bybit API secret (required for live trading) |

### Market Data (Optional)
| Variable | Purpose |
|----------|---------|
| `COINGLASS_API_KEY` | OI, funding rates, liquidations |
| `COINGECKO_API_KEY` | Backup prices + historical data |
| `ETHERSCAN_API_KEY` | Etherscan V2 on-chain data for Ethereum and BSC; holder lists require an API plan that includes Pro endpoints |
| `BSCSCAN_API_KEY` | Optional BscScan community-endpoint fallback when no Etherscan V2 key is set |
| `SOLSCAN_API_KEY` | Solana on-chain data |
| `TWITTER_BEARER_TOKEN` | Optional X mention data for social activity |

### Bot Settings
| Variable | Default | Purpose |
|----------|---------|---------|
| `SCAN_INTERVAL_SECONDS` | 30 | Autonomous scan frequency |
| `ALERT_COOLDOWN_MINUTES` | 15 | Min time between duplicate alerts |
| `MIN_CONFIDENCE_ALERT` | 0.5 | Minimum confidence to send alert |
| `MIN_LAYERS_CONFIRMED` | 2 | Min detection layers for alert |

## Technical Analysis Engine

- **RSI** (Relative Strength Index) — overbought/oversold detection
- **EMA Crossovers** (9/21 period) — trend direction
- **MACD** — momentum analysis
- **Bollinger Bands** — volatility and price position
- **Candlestick Patterns** — Doji, Hammer, Shooting Star, Bullish/Bearish Engulfing, Morning/Evening Star, Three White Soldiers/Black Crows
- **Support/Resistance Levels** — automated detection from price history
- **Direction Stickiness** — prevents flip-flopping, needs 20%+ signal strength to change

## Detection Layers

1. **On-Chain Supply Control** — Wallet concentration, cold wallet withdrawals, accumulation patterns
2. **Perp/Index Manipulation** — OI-to-market-cap ratio, fake OI, price suppression
3. **Funding Rate Dynamics** — Rate depth, trend analysis, cross-exchange divergence
4. **Spot/Order Book Manipulation** — Liquidation cascades, spoof orders, volume anomalies

## Risk Management

- Daily loss circuit breaker (10% max daily loss stops all trading)
- Correlation filter (max 3 same-chain positions)
- Volatility-based stop loss tightening
- Liquidity-aware position sizing
- Paper trading mode by default
- Two-step confirmation for live trades

## Database

Supports two backends:
- **PostgreSQL (Supabase)** — Recommended for persistent local and hosted data. Copy the Session Pooler URI from Supabase Connect (port 5432) into `DATABASE_URL`.
- **SQLite** — Default fallback for local development. Hosted filesystems may be lost on restarts or redeploys.

## Project Structure

```
Quil/
├── config/              # Settings, exchange configs, thresholds
├── core/                # Data pipeline, chart analysis, signal aggregator, backtester, execution, risk, position monitor
├── data/                # API clients (DexScreener, Coinglass, explorers, social)
├── detection/           # 4 detection layers + lifecycle tracker + orchestrator
├── frameworks/          # hl-trap-bot, Freqtrade, Hummingbot, OctoBot adapters
├── models/              # Token, Alert, Trade data models
├── patterns/            # Historical crime coin pattern database
├── telegram_bot/        # Bot interface, handlers, formatters, keyboards
├── utils/               # Logging, database (SQLite + PostgreSQL)
├── main.py              # Entry point
├── Procfile             # Railway deployment
└── .gitignore
```

## Deployment

Deployed on Railway (~$5/month). Auto-redeploys on every `git push origin main`. Configure environment variables in the Railway dashboard. Uses Supabase PostgreSQL for persistent data storage.
