from __future__ import annotations

import argparse
import asyncio

from ib_async import Stock

from backtest.engine import BacktestEngine
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService
from research.performance import StrategyPerformanceStore
from research.walkforward import WalkForwardResearch
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy


async def run(symbols: list[str], duration: str, bar_size: str) -> None:
    connection = IBKRConnection(config.ibkr)
    store = StrategyPerformanceStore()
    try:
        ib = await connection.connect()
        history = HistoricalDataService(ib)
        research = WalkForwardResearch(store, BacktestEngine())
        strategies = [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]

        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            try:
                qualified = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
                if not qualified:
                    print(f"{symbol}: SKIPPED could_not_qualify")
                    continue
                bars = await history.bars(qualified[0], duration=duration, bar_size=bar_size)
                if len(bars) < 320:
                    print(f"{symbol}: SKIPPED insufficient_history bars={len(bars)}")
                    continue

                run_id = store.begin_research_run(
                    symbol=symbol, asset_class="STK", timeframe=bar_size,
                    bars=len(bars), dataset_start=str(bars[0].time), dataset_end=str(bars[-1].time),
                    source="BULK_HISTORICAL", strategy_version="v1",
                )
                try:
                    summaries = research.evaluate(
                        symbol=symbol, asset_class="STK", timeframe=bar_size,
                        bars=bars, strategies=strategies, run_id=run_id,
                        source="BULK_HISTORICAL", strategy_version="v1",
                    )
                    oos = sum(item.oos_windows for item in summaries)
                    if oos > 0:
                        store.finish_research_run(run_id, status="COMPLETED")
                        store.mark_researched(
                            symbol=symbol, asset_class="STK", timeframe=bar_size,
                            bars=len(bars), run_id=run_id,
                        )
                        print(f"{symbol}: COMPLETED bars={len(bars)} oos_windows={oos} run_id={run_id}")
                    else:
                        store.finish_research_run(run_id, status="FAILED", notes="no_oos_windows")
                        print(f"{symbol}: FAILED no_oos_windows run_id={run_id}")
                except Exception as exc:
                    store.finish_research_run(run_id, status="FAILED", notes=str(exc)[:500])
                    print(f"{symbol}: FAILED {exc}")
            except Exception as exc:
                # One bad symbol/data request must not abort the batch.
                print(f"{symbol}: ERROR {exc}")

        print("\nTOP LEADERBOARD")
        for row in store.leaderboard(asset_class="STK", timeframe=bar_size, limit=30):
            pf = "n/a" if row["avg_pf"] is None else f"{row['avg_pf']:.2f}"
            sh = "n/a" if row["avg_sharpe"] is None else f"{row['avg_sharpe']:.2f}"
            print(
                f"{row['symbol']:8} {row['regime']:16} {row['strategy']:28} "
                f"samples={row['samples']:3d} trades={row['trades']:4d} "
                f"ret={row['avg_return']:7.2f}% dd={row['avg_dd']:6.2f}% "
                f"pf={pf:>5} sh={sh:>5} oos={row['oos_samples']}"
            )
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk append-only historical walk-forward/OOS research for arbitrary symbols"
    )
    parser.add_argument("symbols", nargs="+", help="Symbols to research; no permanent whitelist is stored")
    parser.add_argument("--duration", default="365 D")
    parser.add_argument("--bar-size", default="1 hour")
    args = parser.parse_args()
    asyncio.run(run(args.symbols, args.duration, args.bar_size))


if __name__ == "__main__":
    main()
