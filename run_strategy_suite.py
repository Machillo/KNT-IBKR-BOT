from __future__ import annotations

import argparse
import asyncio

from ib_async import Stock

from backtest.engine import BacktestEngine
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy


async def run(symbol: str, duration: str, bar_size: str) -> None:
    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        qualified = await ib.qualifyContractsAsync(Stock(symbol.upper(), "SMART", "USD"))
        if not qualified:
            raise RuntimeError(f"Could not qualify {symbol}")
        bars = await HistoricalDataService(ib).bars(
            qualified[0], duration=duration, bar_size=bar_size
        )
        strategies = [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]
        engine = BacktestEngine()
        results = []
        for strategy in strategies:
            result = engine.run(bars, strategy)
            results.append((strategy.name, result))
        results.sort(key=lambda item: item[1].total_return_pct, reverse=True)

        print(f"symbol={symbol.upper()} bars={len(bars)} duration={duration} bar_size={bar_size}")
        print("strategy                     return     maxDD  trades    win%       PF   sharpe")
        print("-" * 82)
        for name, r in results:
            pf = "n/a" if r.profit_factor is None else f"{r.profit_factor:.2f}"
            sh = "n/a" if r.sharpe is None else f"{r.sharpe:.2f}"
            print(f"{name:28} {r.total_return_pct:8.2f}% {r.max_drawdown_pct:8.2f}% {r.trades:6d} {r.win_rate_pct:7.2f}% {pf:>8} {sh:>8}")
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest all single-asset KNT strategy families")
    parser.add_argument("symbol")
    parser.add_argument("--duration", default="180 D")
    parser.add_argument("--bar-size", default="1 hour")
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.duration, args.bar_size))


if __name__ == "__main__":
    main()
