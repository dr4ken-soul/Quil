# Quil - Crime Pump Detection & Trading Analysis Bot

Autonomous crypto trading bot with a four-layer crime pump detection system, expert-level technical analysis, dual-mode backtesting, and real-time position monitoring. Features Bybit real-time price feeds, 8 candlestick pattern recognition, and support/resistance detection.

## Build Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Foundation (config, models, database, logging) | ✅ Complete |
| 2 | Data layer (Bybit, CoinGecko, DexScreener, Coinglass, explorers) | ✅ Complete |
| 3 | Crime pump detection (4 layers + lifecycle tracker) | ✅ Complete |
| 4 | hl-trap-bot 5-gate pipeline | ✅ Complete |
| 5 | Framework adapters (Freqtrade, Hummingbot, OctoBot) | ✅ Complete |
| 6 | Signal aggregation + execution engine + risk manager | ✅ Complete |
| 7 | Telegram bot interface (all commands) | ✅ Complete |
| 8 | Autonomous market scanning | ✅ Complete |
| 9 | Position monitoring & alerts (TP/SL/liquidation/custom) | ✅ Complete |
| 10 | Backtesting engine (Trade + Crime Pump modes) | ✅ Complete |
| 10.5 | Expert chart analysis + Bybit price pipeline + bug fixes | ✅ Complete |
| — | **Deployed on Railway (24/7)** | ✅ Live |
| 11 | Live execution (Execute Trade button + /sell) | ❌ Next |
| 12 | Performance dashboard (/stats) | ❌ After 11 |

## Two Main Features

### 1. Crime Pump Detection
Scans the market continuously for coordinated manipulation across 4 detection layers (on-chain supply, perp/index manipulation, funding rate dynamics, order book manipulation). Classifies tokens into lifecycle stages (accumulation → OI matrix → trap → distribution).

### 2. Trading Analysis
Expert-level technical analysis engine that reads charts independently. Uses RSI, EMA Crossovers, MACD, Bollinger Bands, 8 candlestick patterns, and support/resistance levels to generate trade signals with direction (Long/Short), entry, SL, TP1, TP2, leverage, and position sizing. Crime pump detection is a **warning overlay only** — it does not drive trade direction.

## Price Pipeline

```
Token Price Resolution (priority order):
1. Bybit Public API    → Real-time exchange price (no key needed)
2. CoinGecko Search    → Dynamic symbol search (works for ANY token)
3. DexScreener         → DEX pool price (fallback for DEX-only tokens)
```

## Telegram Commands

| Command | What It Does |
|---------|-------------|
| `/scan [ticker]` | Crime pump analysis (4 detection layers) |
| `/trade [ticker]` | Full trade analysis with entry/SL/TP/leverage |
| `/analyze [ticker]` | Alias for /trade |
| `/calc [ticker] [amount] [leverage]` | Calculate profit/loss for a trade setup |
| `/backtest [ticker] [period]` | Trade analysis backtesting |
| `/backtest [ticker] [period] crime` | Crime pump detection backtesting |
| `/track [ticker] [entry] [sl] [tp] [leverage]` | Monitor a position for alerts |
| `/positions` | View all tracked positions with live PnL |
| `/close [ticker]` | Stop tracking a position |
| `/setalert [ticker] [price]` | Custom price alert |
| `/watch [ticker]` | Add to continuous monitoring (no duplicates) |
| `/watchlist` | View monitored tokens |
| `/alerts` | View recent detections |
| `/risk` | Portfolio risk status and circuit breaker |
| `/status` | Data source health check |

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

## Required API Keys

| Service | Required | Purpose |
|---------|----------|---------|
| Telegram Bot Token | Yes | Bot interface |
| Bybit | No (free public API) | Real-time prices |
| CoinGecko | No (free public API) | Backup prices + historical data |
| DexScreener | No (free) | DEX data, volume, pairs, metadata |
| Coinglass | Recommended | OI, funding rates, liquidations |
| Etherscan | Optional | EVM on-chain data, contract age |
| Solscan | Optional | Solana on-chain data |
| Exchange APIs | Optional | Order book data and trade execution |

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

## Project Structure

```
Quil/
├── config/              # Settings, exchange configs, thresholds
├── core/                # Data pipeline, chart analysis, signal aggregator, backtester, execution, risk
├── data/                # API clients (DexScreener, Coinglass, explorers, social)
├── detection/           # 4 detection layers + lifecycle tracker + orchestrator
├── frameworks/          # hl-trap-bot, Freqtrade, Hummingbot, OctoBot adapters
├── models/              # Token, Alert, Trade data models
├── patterns/            # Historical crime coin pattern database
├── telegram_bot/        # Bot interface, handlers, formatters, keyboards
├── utils/               # Logging, database
├── main.py              # Entry point
├── Procfile             # Railway deployment
└── .gitignore
```

## Deployment

Deployed on Railway (~$5/month). Auto-redeploys on every `git push origin main`. Configure environment variables in the Railway dashboard.
