# Quil - Crime Pump Detection & Trading Bot

Autonomous crypto trading bot with a four-layer crime pump detection system, trade signal generation, position monitoring, and real-time alerts. Monitors DEX activity, derivatives markets, on-chain data, and social signals to detect coordinated manipulation patterns and generate actionable trade recommendations.

## Build Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Foundation (config, models, database, logging) | ✅ Complete |
| 2 | Data layer (DexScreener, Coinglass, explorers, social) | ✅ Complete |
| 3 | Crime pump detection (4 layers + lifecycle tracker) | ✅ Complete |
| 4 | hl-trap-bot 5-gate pipeline | ✅ Complete |
| 5 | Framework adapters (Freqtrade, Hummingbot, OctoBot) | ✅ Complete |
| 6 | Signal aggregation + execution engine + risk manager | ✅ Complete |
| 7 | Telegram bot interface (all commands) | ✅ Complete |
| 8 | Autonomous market scanning | ✅ Complete |
| 9 | Position monitoring & alerts (TP/SL/liquidation/custom) | ✅ Complete |
| 10 | Backtesting engine | ❌ Not built |
| 11 | Live execution testing | ❌ Not tested |
| 12 | Performance dashboard | ❌ Not built |

## What It Does

**Crime Pump Detection:** Scans the market continuously for coordinated manipulation across 4 detection layers (on-chain supply, perp/index manipulation, funding rate dynamics, order book manipulation). Classifies tokens into lifecycle stages (accumulation → OI matrix → trap → distribution).

**Trade Signal Generation:** Combines crime pump detection, trap bot analysis, and technical indicators to generate trade recommendations with direction (Long/Short), entry price, stop loss, take profit levels, leverage (2x-10x), and position size.

**Position Monitoring:** Tracks your open positions in real-time and sends Telegram alerts when TP is hit, SL is hit, you're about to be liquidated, or your position reaches profit milestones (50%, 100%, 200%, x2, x5, x10).

## Telegram Commands

| Command | What It Does |
|---------|-------------|
| `/scan [ticker]` | Crime pump analysis (4 detection layers) |
| `/trade [ticker]` | Full trade analysis with entry/SL/TP/leverage |
| `/analyze [ticker]` | Alias for /trade |
| `/calc [ticker] [amount] [leverage]` | Calculate profit/loss for a trade setup |
| `/track [ticker] [entry] [sl] [tp] [leverage]` | Monitor a position for alerts |
| `/positions` | View all tracked positions with live PnL |
| `/close [ticker]` | Stop tracking a position |
| `/setalert [ticker] [price]` | Custom price alert |
| `/watch [ticker]` | Add to continuous monitoring |
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
| DexScreener | No (free) | DEX data, volume, pairs |
| Coinglass | Recommended | OI, funding rates, liquidations |
| Etherscan | Optional | EVM on-chain data |
| BscScan | Optional | BSC on-chain data |
| Solscan | Optional | Solana on-chain data |
| Twitter/X | Optional | Social signal monitoring |
| Exchange APIs | Optional | Order book data and trade execution |

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
- Paper trading mode by default (no real money at risk)

## Project Structure

```
Quil/
├── config/              # Settings, exchange configs, thresholds
├── core/                # Data pipeline, signal aggregation, execution, risk, position monitor
├── data/                # API clients (DexScreener, Coinglass, explorers, social)
├── detection/           # 4 detection layers + lifecycle tracker + orchestrator
├── frameworks/          # hl-trap-bot, Freqtrade, Hummingbot, OctoBot adapters
├── models/              # Token, Alert, Trade data models
├── patterns/            # Historical crime coin pattern database
├── telegram_bot/        # Bot interface, handlers, formatters, keyboards
├── utils/               # Logging, database
└── main.py              # Entry point
```

## Deployment

Recommended: Railway (~$5/month). Add a `Procfile` with `worker: python main.py` and configure environment variables in the Railway dashboard.
