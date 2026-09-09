from __future__ import annotations

import argparse
import asyncio

from ib_async import Stock

from backtest.engine import BacktestEngine
from backtest.growth import simulate_growth
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy


def strategy_map() -> dict[str, object]:
    items = [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]
    return {item.name: item for item in items}


async def run(symbol: str, strategy_name: str, duration: str, bar_size: str,
              future_trades: int, paths: int) -> None:
    available = strategy_map()
    if strategy_name not in available:
        raise ValueError(f"Unknown strategy {strategy_name}; choices={sorted(available)}")

    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        qualified = await ib.qualifyContractsAsync(Stock(symbol.upper(), "SMART", "USD"))
        if not qualified:
            raise RuntimeError(f"Could not qualify {symbol}")
        bars = await HistoricalDataService(ib).bars(
            qualified[0], duration=duration, bar_size=bar_size
        )
        result = BacktestEngine().run(bars, available[strategy_name])
        returns = [trade.return_pct / 100.0 for trade in result.trade_log]
        if len(returns) < 10:
            raise RuntimeError(f"Insufficient trades for projection: {len(returns)}")
        projection = simulate_growth(
            returns,
            starting_equity=result.initial_equity,
            periods=future_trades,
            paths=paths,
        )
        print(
            f"BACKTEST {symbol.upper()} {strategy_name} trades={result.trades} "
            f"ret={result.total_return_pct:.2f}% dd={result.max_drawdown_pct:.2f}%"
        )
        print(
            f"PROJECTION future_trades={future_trades} paths={paths} "
            f"median=${projection.median_final_equity:,.2f} "
            f"p10=${projection.p10_final_equity:,.2f} p90=${projection.p90_final_equity:,.2f} "
            f"loss_probability={projection.probability_of_loss_pct:.1f}% "
            f"ruin_probability={projection.probability_of_ruin_pct:.1f}%"
        )
        print("Projection bootstraps historical net trade returns; it is not a forecast or guarantee.")
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap KNT account growth from backtested net trade returns")
    parser.add_argument("symbol")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--duration", default="5 Y")
    parser.add_argument("--bar-size", default="4 hours")
    parser.add_argument("--future-trades", type=int, default=250)
    parser.add_argument("--paths", type=int, default=5000)
    args = parser.parse_args()
    asyncio.run(run(args.symbol, args.strategy, args.duration, args.bar_size, args.future_trades, args.paths))


if __name__ == "__main__":
    main()
