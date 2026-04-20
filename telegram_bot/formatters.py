"""
Telegram Bot Message Formatters

Formats crime pump alerts, trade analyses, and scan results into
clean, structured Telegram messages following the defined output formats.

All outputs follow the texting style rules: minimal punctuation where unnecessary,
natural word flow, proper grammar, cohesive paragraphs, no em dashes.
"""

from models.alert import CrimePumpAlert
from models.trade import TradeSignal
from datetime import datetime


def format_crime_pump_alert(alert: CrimePumpAlert) -> str:
    """
    Format a crime pump alert for Telegram output.

    Follows the exact specification from the project requirements.
    """
    lines = []

    # Header
    if alert.is_confirmed:
        lines.append("🚨 Crime Pump Alert")
    else:
        lines.append("⚠️ Crime Pump Watch")

    lines.append("")
    lines.append(f"Token: ${alert.token_symbol}")
    if alert.contract_address:
        lines.append(f"Contract: {alert.contract_address[:10]}...{alert.contract_address[-6:]}")
    if alert.chain:
        lines.append(f"Chain: {alert.chain.title()}")
    lines.append(f"Confidence: {alert.confidence_level.value}")
    lines.append(f"Stage: {alert.crime_stage.value}")

    # Detection layers triggered
    lines.append("")
    lines.append("Detection Layers Triggered:")
    if alert.layers_triggered:
        for lr in alert.layer_results:
            if lr.triggered:
                # Get the top signal from this layer
                top_signal = lr.signals[0] if lr.signals else {"observation": "Anomaly detected"}
                lines.append(f"  Layer {lr.layer_number}: {lr.layer_name}")
                lines.append(f"  └ {top_signal.get('observation', 'Signal detected')}")
    else:
        lines.append("  None")

    # Signals detected
    if alert.signals_detected:
        lines.append("")
        lines.append("Signals Detected:")
        for i, sig in enumerate(alert.signals_detected[:5], 1):
            lines.append(f"  {i}. {sig.get('name', 'Unknown')}")
            obs = sig.get('observation', '')
            if obs:
                lines.append(f"     {obs}")

    # Market snapshot
    lines.append("")
    lines.append("Market Snapshot:")
    lines.append(f"  Price: ${alert.price:.6f}" if alert.price < 1 else f"  Price: ${alert.price:,.2f}")
    lines.append(f"  Market Cap: ${alert.market_cap:,.0f}")
    lines.append(f"  Liquidity: ${alert.liquidity:,.0f}")

    if alert.oi_to_mcap_ratio > 0:
        lines.append(f"  OI/MCap: {alert.oi_to_mcap_ratio:.2f}")
    if alert.funding_rate != 0:
        lines.append(f"  Funding Rate: {alert.funding_rate * 100:.2f}%")
    if alert.top10_wallet_concentration > 0:
        lines.append(f"  Top 10 Wallets: {alert.top10_wallet_concentration:.1f}%")
    if alert.primary_exchange:
        lines.append(f"  Primary Exchange: {alert.primary_exchange.title()}")

    # Recommendation
    lines.append("")
    lines.append(f"Recommendation: {alert.recommendation.value}")

    # Reasoning summary (truncated for Telegram)
    if alert.full_reasoning:
        reasoning_short = alert.full_reasoning[:300]
        if len(alert.full_reasoning) > 300:
            reasoning_short += "..."
        lines.append("")
        lines.append(f"Reasoning: {reasoning_short}")

    # Timestamp
    lines.append("")
    lines.append(f"Scanned: {alert.created_at.strftime('%Y-%m-%d %H:%M UTC')}")

    return "\n".join(lines)


def format_refreshed_alert(alert: CrimePumpAlert) -> str:
    """
    Format a refreshed analysis showing what changed.
    """
    lines = []

    lines.append(f"🔄 Refreshed Analysis: ${alert.token_symbol}")
    lines.append(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    # Full analysis
    lines.append(format_crime_pump_alert(alert))

    # Changes from previous
    if alert.changes_from_previous:
        lines.append("")
        lines.append("Changes from Last Analysis:")
        for change in alert.changes_from_previous:
            lines.append(f"  • {change}")
    else:
        lines.append("")
        lines.append("No significant changes since last analysis.")

    return "\n".join(lines)


def format_trade_analysis(signal: TradeSignal) -> str:
    """
    Format a complete trade analysis for Telegram output.
    Includes direction, entry/SL/TP, leverage, position size,
    technical signals, on-chain data, trap detection,
    crime pump status, and full reasoning.
    """
    lines = []

    # Direction emoji and header
    dir_emoji = "📈" if signal.direction.value == "LONG" else "📉"
    dir_word = signal.direction.value

    lines.append(f"{dir_emoji} Trade Analysis: ${signal.token_symbol}")
    lines.append("")

    # Primary trade recommendation block
    lines.append(f"Direction: {dir_word}")
    lines.append(f"Confidence: {signal.confidence:.0%}")
    lines.append(f"Timeframe: {signal.timeframe}")

    # Risk parameters (the key numbers)
    risk = signal.risk
    lines.append("")
    lines.append("Trade Setup:")

    if risk.entry_price > 0:
        entry_str = f"${risk.entry_price:.6f}" if risk.entry_price < 1 else f"${risk.entry_price:,.2f}"
        lines.append(f"  Entry: {entry_str}")

    if risk.stop_loss > 0:
        sl_str = f"${risk.stop_loss:.6f}" if risk.stop_loss < 1 else f"${risk.stop_loss:,.2f}"
        # Calculate SL % distance
        sl_pct = abs(risk.entry_price - risk.stop_loss) / risk.entry_price * 100 if risk.entry_price > 0 else 0
        lines.append(f"  Stop Loss: {sl_str} ({sl_pct:.1f}% away)")

    if risk.take_profit_1 > 0:
        tp1_str = f"${risk.take_profit_1:.6f}" if risk.take_profit_1 < 1 else f"${risk.take_profit_1:,.2f}"
        lines.append(f"  Take Profit 1: {tp1_str}")

    if risk.take_profit_2 > 0:
        tp2_str = f"${risk.take_profit_2:.6f}" if risk.take_profit_2 < 1 else f"${risk.take_profit_2:,.2f}"
        lines.append(f"  Take Profit 2: {tp2_str}")

    if risk.risk_reward > 0:
        lines.append(f"  Risk/Reward: {risk.risk_reward:.1f}:1")

    # Leverage and position sizing
    lines.append("")
    lines.append("Position Sizing:")

    # Calculate leverage from confidence and volatility
    if signal.confidence >= 0.7:
        leverage = 10
    elif signal.confidence >= 0.5:
        leverage = 5
    elif signal.confidence >= 0.4:
        leverage = 3
    else:
        leverage = 2

    # Reduce leverage during crime pump
    if signal.crime_pump_status.value != "None":
        leverage = min(leverage, 5)

    lines.append(f"  Leverage: {leverage}x")
    lines.append(f"  Position Size: {risk.position_size_pct:.1f}% of portfolio")

    if risk.max_loss_usd > 0:
        lines.append(f"  Max Loss: ${risk.max_loss_usd:,.2f}")

    # Technical signals
    if signal.technical_signals:
        lines.append("")
        lines.append("Technical Signals:")
        for ts in signal.technical_signals:
            lines.append(f"  {ts.name}: {ts.observation}")
            if ts.significance:
                lines.append(f"  └ {ts.significance}")

    # On-chain signals
    if signal.onchain_signals:
        lines.append("")
        lines.append("On-Chain Signals:")
        for os_sig in signal.onchain_signals:
            lines.append(f"  {os_sig.get('name', 'Signal')}: "
                        f"{os_sig.get('observation', '')}")

    # Market structure
    if signal.market_structure_summary:
        lines.append("")
        lines.append(f"Market Structure: {signal.market_structure_summary}")

    # Trap detection
    td = signal.trap_detection
    if td.trap_fired.value != "None" or td.gate_trace:
        lines.append("")
        lines.append("Trap Detection (hl-trap-bot):")
        lines.append(f"  Regime: {td.regime.value}")
        lines.append(f"  Trap Fired: {td.trap_fired.value}")
        if td.confidence > 0:
            lines.append(f"  Trap Confidence: {td.confidence:.0%}")
        if td.gate_trace:
            lines.append(f"  Gate Trace: {td.gate_trace}")

    # Crime pump status
    if signal.crime_pump_status.value != "None":
        lines.append("")
        if signal.crime_pump_override:
            lines.append(f"⚠️ CRIME PUMP OVERRIDE: {signal.crime_pump_status.value}")
            lines.append("  Trade direction may conflict with manipulation flow.")
        else:
            lines.append(f"Crime Pump: {signal.crime_pump_status.value}")

    # Reasoning
    if signal.reasoning_summary:
        lines.append("")
        lines.append(f"Reasoning: {signal.reasoning_summary}")

    # Framework attribution
    if signal.contributing_frameworks:
        lines.append("")
        lines.append(f"Sources: {', '.join(signal.contributing_frameworks)}")

    return "\n".join(lines)


def format_scan_summary(tokens_scanned: int, alerts_generated: int,
                         watch_alerts: int, top_alerts: list[CrimePumpAlert]) -> str:
    """Format a scan cycle summary."""
    lines = []

    lines.append("📡 Scan Cycle Complete")
    lines.append("")
    lines.append(f"Tokens scanned: {tokens_scanned}")
    lines.append(f"Crime pump alerts: {alerts_generated}")
    lines.append(f"Watch alerts: {watch_alerts}")

    if top_alerts:
        lines.append("")
        lines.append("Top Detections:")
        for alert in top_alerts[:3]:
            lines.append(f"  ${alert.token_symbol} - {alert.confidence_level.value} "
                        f"({alert.total_layers_triggered} layers)")

    lines.append("")
    lines.append(f"Time: {datetime.utcnow().strftime('%H:%M UTC')}")

    return "\n".join(lines)


def format_no_signals(token_symbol: str) -> str:
    """Format a response when no crime pump signals are detected."""
    return (
        f"🟢 Scan Complete: ${token_symbol}\n"
        f"\n"
        f"No crime pump signals detected.\n"
        f"\n"
        f"All four detection layers were evaluated:\n"
        f"  Layer 1 (On-Chain Supply Control): Clear\n"
        f"  Layer 2 (Perp/Index Manipulation): Clear\n"
        f"  Layer 3 (Funding Rate Dynamics): Clear\n"
        f"  Layer 4 (Spot/Order Book Manipulation): Clear\n"
        f"\n"
        f"The token does not match any known crime pump patterns at this time. "
        f"This assessment is based on current data and should be rechecked "
        f"if market conditions change significantly.\n"
        f"\n"
        f"Scanned: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )


def format_degraded_scan_notice(token_symbol: str,
                                 failed_sources: list[str]) -> str:
    """Format a notice when scan operates with incomplete data."""
    return (
        f"⚠️ Degraded Scan: ${token_symbol}\n"
        f"\n"
        f"The following data sources are currently unavailable:\n"
        f"{''.join(f'  • {s}' + chr(10) for s in failed_sources)}"
        f"\n"
        f"A confirmed crime pump alert cannot be issued with fewer than "
        f"two evaluable detection layers. This is a data integrity safeguard.\n"
        f"\n"
        f"The scan will automatically retry when data sources recover."
    )
