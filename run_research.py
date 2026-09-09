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


async def run(symbol: str, duration: str, bar_size: str) -> None:
    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        qualified = await ib.qualifyContractsAsync(Stock(symbol.upper(), "SMART", "USD"))
        if not qualified:
            raise RuntimeError(f"Could not qualify {symbol}")
        bars = await HistoricalDataService(ib).bars(qualified[0], duration=duration, bar_size=bar_size)
        store = StrategyPerformanceStore()
        dataset_start = None if not bars else str(bars[0].time)
        dataset_end = None if not bars else str(bars[-1].time)
        run_id = store.begin_research_run(
            symbol=symbol.upper(), asset_class="STK", timeframe=bar_size,
            bars=len(bars), dataset_start=dataset_start, dataset_end=dataset_end,
            source="RESEARCH", strategy_version="v1",
        )
        research = WalkForwardResearch(store, BacktestEngine())
        strategies = [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]
        try:
            summaries = research.evaluate(
                symbol=symbol.upper(), asset_class="STK", timeframe=bar_size,
                bars=bars, strategies=strategies, run_id=run_id,
                source="RESEARCH", strategy_version="v1",
            )
            if any(item.oos_windows > 0 for item in summaries):
                store.finish_research_run(run_id, status="COMPLETED")
                store.mark_researched(
                    symbol=symbol.upper(), asset_class="STK", timeframe=bar_size,
                    bars=len(bars), run_id=run_id,
                )
            else:
                store.finish_research_run(run_id, status="FAILED", notes="no_oos_windows")
        except Exception as exc:
            store.finish_research_run(run_id, status="FAILED", notes=str(exc)[:500])
            raise

        print(f"research symbol={symbol.upper()} bars={len(bars)} timeframe={bar_size} run_id={run_id}")
        for item in summaries:
            print(f"{item.strategy:28} train={item.train_windows:3d} oos={item.oos_windows:3d}")
        print("\nleaderboard")
        for row in store.leaderboard(asset_class="STK", timeframe=bar_size, limit=20):
            pf = "n/a" if row["avg_pf"] is None else f"{row['avg_pf']:.2f}"
            sh = "n/a" if row["avg_sharpe"] is None else f"{row['avg_sharpe']:.2f}"
            print(
                f"{row['symbol']:6} {row['regime']:16} {row['strategy']:28} "
                f"samples={row['samples']:3d} trades={row['trades']:4d} "
                f"ret={row['avg_return']:7.2f}% dd={row['avg_dd']:6.2f}% pf={pf:>5} sh={sh:>5} oos={row['oos_samples']}"
            )
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build KNT append-only walk-forward evidence")
    parser.add_argument("symbol")
    parser.add_argument("--duration", default="365 D")
    parser.add_argument("--bar-size", default="1 hour")
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.duration, args.bar_size))


if __name__ == "__main__":
    main()
