from __future__ import annotations

import argparse
import asyncio

from ib_async import Stock

from backtest.engine import BacktestEngine
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService
from strategies.momentum import MomentumStrategy


async def run(symbol: str, duration: str, bar_size: str) -> None:
    config.validate()
    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        qualified = await ib.qualifyContractsAsync(Stock(symbol.upper(), "SMART", "USD"))
        if not qualified:
            raise RuntimeError(f"Could not qualify {symbol}")
        bars = await HistoricalDataService(ib).bars(qualified[0], duration, bar_size)
        result = BacktestEngine().run(bars, MomentumStrategy())
        print(f"symbol={symbol.upper()} bars={len(bars)} trades={result.trades}")
        print(f"equity={result.initial_equity:.2f} -> {result.final_equity:.2f}")
        print(f"return={result.total_return_pct:.2f}% max_dd={result.max_drawdown_pct:.2f}%")
        print(f"wins={result.wins} losses={result.losses} win_rate={result.win_rate_pct:.2f}%")
        print(f"profit_factor={result.profit_factor} sharpe={result.sharpe}")
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="KNT IBKR baseline backtest")
    parser.add_argument("symbol")
    parser.add_argument("--duration", default="30 D")
    parser.add_argument("--bar-size", default="1 hour")
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.duration, args.bar_size))


if __name__ == "__main__":
    main()
