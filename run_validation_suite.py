from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from ib_async import Stock

from backtest.engine import BacktestEngine
from backtest.validation import run_validation_matrix, robustness_score, write_validation_report
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService
from research.performance import StrategyPerformanceStore
from research.walkforward import WalkForwardResearch
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy


@dataclass(frozen=True)
class ValidationProfile:
    name: str
    duration: str
    bar_size: str


PROFILES = (
    ValidationProfile("intraday_1y", "1 Y", "1 hour"),
    ValidationProfile("swing_5y", "5 Y", "4 hours"),
    ValidationProfile("long_10y", "10 Y", "1 day"),
)


def strategies() -> list[object]:
    return [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]


async def run(symbols: list[str], output_dir: str) -> None:
    connection = IBKRConnection(config.ibkr)
    store = StrategyPerformanceStore()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    all_rows = []
    try:
        ib = await connection.connect()
        history = HistoricalDataService(ib)
        for raw in symbols:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            qualified = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
            if not qualified:
                print(f"{symbol}: SKIPPED could_not_qualify")
                continue
            contract = qualified[0]
            for profile in PROFILES:
                try:
                    bars = await history.bars(contract, duration=profile.duration, bar_size=profile.bar_size)
                except Exception as exc:
                    print(f"{symbol} {profile.name}: ERROR history {exc}")
                    continue
                if len(bars) < 320:
                    print(f"{symbol} {profile.name}: SKIPPED insufficient_history bars={len(bars)}")
                    continue

                rows = run_validation_matrix(
                    symbol=symbol,
                    timeframe=profile.bar_size,
                    duration=profile.duration,
                    bars=bars,
                    strategies=strategies(),
                )
                all_rows.extend(rows)
                stem = f"{symbol}_{profile.name}"
                write_validation_report(rows, out, stem)

                baseline = [row for row in rows if row.scenario == "baseline"]
                best = max(baseline, key=lambda row: row.total_return_pct)
                score = robustness_score(rows)
                print(
                    f"{symbol} {profile.name}: bars={len(bars)} best={best.strategy} "
                    f"ret={best.total_return_pct:.2f}% dd={best.max_drawdown_pct:.2f}% "
                    f"trades={best.trades} benchmark={best.buy_hold_return_pct:.2f}% robustness={score:.1f}"
                )

                run_id = store.begin_research_run(
                    symbol=symbol,
                    asset_class="STK",
                    timeframe=profile.bar_size,
                    bars=len(bars),
                    dataset_start=str(bars[0].time),
                    dataset_end=str(bars[-1].time),
                    source="VALIDATION_SUITE",
                    strategy_version="v1",
                )
                research = WalkForwardResearch(store, BacktestEngine())
                try:
                    summaries = research.evaluate(
                        symbol=symbol,
                        asset_class="STK",
                        timeframe=profile.bar_size,
                        bars=bars,
                        strategies=strategies(),
                        run_id=run_id,
                        source="VALIDATION_SUITE",
                        strategy_version="v1",
                    )
                    oos = sum(item.oos_windows for item in summaries)
                    if oos:
                        store.finish_research_run(run_id, status="COMPLETED")
                        store.mark_researched(
                            symbol=symbol,
                            asset_class="STK",
                            timeframe=profile.bar_size,
                            bars=len(bars),
                            run_id=run_id,
                        )
                    else:
                        store.finish_research_run(run_id, status="FAILED", notes="no_oos_windows")
                    print(f"  walkforward: oos_windows={oos} run_id={run_id}")
                except Exception as exc:
                    store.finish_research_run(run_id, status="FAILED", notes=str(exc)[:500])
                    print(f"  walkforward: FAILED {exc}")

        if all_rows:
            write_validation_report(all_rows, out, "ALL_RESULTS")
            print(f"\nReports written to: {out.resolve()}")
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="KNT multi-horizon strategy/symbol robustness validation")
    parser.add_argument("symbols", nargs="+", help="Arbitrary stocks/ETFs for this validation batch")
    parser.add_argument("--output-dir", default="reports/backtests")
    args = parser.parse_args()
    asyncio.run(run(args.symbols, args.output_dir))


if __name__ == "__main__":
    main()
