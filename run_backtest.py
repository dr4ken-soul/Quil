"""
Backtest runner - runs backtests locally and prints results for tuning.
"""
import asyncio
import sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

from core.backtester import BacktestEngine
from core.signal_aggregator import SignalAggregator


async def main():
    aggregator = SignalAggregator()
    backtester = BacktestEngine(signal_aggregator=aggregator)

    tokens = ["BTC", "SOL", "ETH"]

    for symbol in tokens:
        print(f"\n{'='*60}")
        print(f"  BACKTEST: ${symbol} over 30 days")
        print(f"{'='*60}")

        result = await backtester.run_backtest(symbol, days=30)

        if result.error:
            print(f"  ERROR: {result.error}")
            continue

        print(f"  Start Price: ${result.start_price:,.2f}")
        print(f"  End Price:   ${result.end_price:,.2f}")
        print(f"  Buy & Hold:  {result.buy_hold_pnl_pct:+.1f}%")
        print(f"")
        print(f"  Total PnL:   {result.total_pnl_pct:+.1f}% (${result.total_pnl_usd:+.2f})")
        print(f"  Total Trades: {result.total_trades}")
        print(f"  Win Rate:    {result.win_rate:.1f}%")
        print(f"  Wins: {result.winning_trades} | Losses: {result.losing_trades}")
        print(f"  Best Trade:  {result.best_trade_pct:+.1f}%")
        print(f"  Worst Trade: {result.worst_trade_pct:+.1f}%")
        print(f"  Max Drawdown: {result.max_drawdown_pct:.1f}%")
        print(f"  Avg Win:     {result.avg_win_pct:+.1f}%")
        print(f"  Avg Loss:    {result.avg_loss_pct:+.1f}%")
        print(f"  Profit Factor: {result.profit_factor:.2f}")
        print(f"  Avg Duration: {result.avg_trade_duration_h:.1f}h")

        if result.trades:
            print(f"\n  Individual Trades:")
            for t in result.trades:
                tag = "[WIN]" if t.pnl_pct > 0.5 else "[LOSS]" if t.pnl_pct < -0.5 else "[BE]"
                entry_str = f"${t.entry_price:,.2f}" if t.entry_price >= 1 else f"${t.entry_price:.6f}"
                exit_str = f"${t.exit_price:,.2f}" if t.exit_price >= 1 else f"${t.exit_price:.6f}"
                print(f"    {tag} {t.direction} | Entry: {entry_str} -> Exit: {exit_str} | PnL: {t.pnl_pct:+.1f}% | {t.exit_reason}")

    await backtester.close()
    print(f"\n{'='*60}")
    print("  BACKTEST COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
