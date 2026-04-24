"""
Quick test script - runs the trade analysis on IRYS, OPG, EDGE
to verify multi-timeframe chart reading is working.
"""
import asyncio
import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv

load_dotenv()

from core.data_pipeline import DataPipeline
from core.signal_aggregator import SignalAggregator


async def test_token(symbol, pipeline, aggregator):
    print(f"\n{'='*60}")
    print(f"  TESTING: ${symbol}")
    print(f"{'='*60}")

    token = await pipeline.build_token_profile(symbol)
    if not token:
        print(f"  [X] Could not find data for ${symbol}")
        return

    print(f"  Price: ${token.metrics.price}")
    print(f"  24h Change: {token.metrics.price_change_24h:+.1f}%")
    print(f"  1h Change: {token.metrics.price_change_1h:+.1f}%")
    print(f"  Market Cap: ${token.metrics.market_cap:,.0f}")

    print(f"\n  Fetching candle data...")
    multi_tf = await pipeline.fetch_multi_timeframe_candles(symbol)

    if multi_tf:
        for tf, candles in multi_tf.items():
            print(f"    {tf}: {len(candles)} candles fetched [OK]")
    else:
        print(f"    [!] No candle data available (DexScreener-only token)")

    print(f"\n  Running analysis...")
    signal = await aggregator.generate_trade_signal(
        token=token,
        multi_tf_data=multi_tf if multi_tf else None,
    )

    print(f"\n  === RESULT ===")
    print(f"  Direction: {signal.direction.value.upper()}")
    print(f"  Confidence: {signal.confidence:.0%}")
    print(f"  Timeframe: {signal.timeframe}")

    if signal.risk:
        price_fmt = lambda p: f"${p:,.6f}" if p < 1 else f"${p:,.4f}"
        print(f"  Entry: {price_fmt(signal.risk.entry_price)}")
        print(f"  Stop Loss: {price_fmt(signal.risk.stop_loss)}")
        print(f"  Take Profit: {price_fmt(signal.risk.take_profit_1)}")
        print(f"  Leverage: {signal.risk.leverage}x")
        print(f"  R:R Ratio: {signal.risk.risk_reward:.1f}")

    print(f"\n  Technical Signals ({len(signal.technical_signals)}):")
    for sig in signal.technical_signals:
        direction = "BULL" if "bullish" in sig.significance.lower() or "bullish" in sig.name.lower() else \
                    "BEAR" if "bearish" in sig.significance.lower() or "bearish" in sig.name.lower() else "----"
        print(f"    [{direction}] {sig.name} (weight: {sig.weight:.2f})")
        print(f"           {sig.observation}")

    print(f"\n  Reasoning: {signal.reasoning_summary[:300]}...")


async def main():
    pipeline = DataPipeline()
    aggregator = SignalAggregator()

    tokens = ["IRYS", "OPG", "EDGE", "BTC", "SOL"]

    for symbol in tokens:
        try:
            await test_token(symbol, pipeline, aggregator)
        except Exception as e:
            print(f"\n  [X] Error testing {symbol}: {e}")
            import traceback
            traceback.print_exc()

    await pipeline.close()
    print(f"\n{'='*60}")
    print("  TESTING COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
