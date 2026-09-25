from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from backtest.engine import BacktestEngine, BacktestResult, BacktestTrade
from market.history import PriceBar
from market.regime import RegimeDetector
from research.performance import StrategyPerformanceStore
from research.splits import DEFAULT_CONTEXT_BARS, Window, rolling_windows, run_window


@dataclass(frozen=True)
class WalkForwardSummary:
    strategy: str
    windows: int
    train_windows: int
    oos_windows: int
    oos_trades: int = 0


def _trade_group_result(trades: list[BacktestTrade], initial_equity: float) -> BacktestResult:
    """Aggregate a subset of trades (one regime) into a result row.

    Returns compound per-trade returns; drawdown from the compounded trade sequence.
    Daily Sharpe is undefined for a trade subset and left as None.
    """
    equity = peak = initial_equity
    max_dd = 0.0
    for t in trades:
        equity *= 1 + t.return_pct / 100
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
    wins = sum(t.pnl > 0 for t in trades)
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses_abs = abs(sum(t.pnl for t in trades if t.pnl < 0))
    pf = gains / losses_abs if losses_abs > 0 else (None if gains == 0 else float("inf"))
    return BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity,
        total_return_pct=(equity / initial_equity - 1) * 100,
        max_drawdown_pct=max_dd * 100,
        trades=len(trades),
        wins=wins,
        losses=len(trades) - wins,
        win_rate_pct=wins / len(trades) * 100 if trades else 0.0,
        profit_factor=pf,
        sharpe=None,
        trade_log=tuple(trades),
    )


class WalkForwardResearch:
    """Rolling walk-forward that produces ADMISSIBLE out-of-sample evidence.

    * Strategies here have fixed parameters; TRAIN windows are recorded for
      diagnostics only (split='TRAIN', regime='ALL') and never feed decisions.
    * Each OOS window reads up to ``context_bars`` earlier bars for warmup, but
      only trades ENTERED inside the window count.
    * Every OOS trade is labelled with the regime detected on the bars available
      when its signal was generated (the same information the live selector has),
      and evidence rows are stored per (window, regime).
    """

    def __init__(self, store: StrategyPerformanceStore, engine: BacktestEngine | None = None,
                 context_bars: int = DEFAULT_CONTEXT_BARS) -> None:
        self.store = store
        self.engine = engine or BacktestEngine()
        self.regime_detector = RegimeDetector()
        self.context_bars = int(context_bars)

    def _regime_at_signal(self, bars: list[PriceBar], entry_index: int) -> str:
        # Signal was produced from bars[:entry_index] (all completed before the entry bar).
        lo = max(0, entry_index - self.context_bars)
        return self.regime_detector.evaluate(bars[lo:entry_index]).regime.value

    def evaluate(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        bars: list[PriceBar],
        strategies: list[object],
        train_bars: int = 240,
        test_bars: int = 80,
        step_bars: int = 80,
        run_id: int | None = None,
        source: str = "RESEARCH",
        strategy_version: str = "v1",
    ) -> list[WalkForwardSummary]:
        if train_bars < 120 or test_bars < 60 or step_bars < 1:
            raise ValueError("Invalid walk-forward window sizes")
        pairs = rolling_windows(len(bars), train=train_bars, test=test_bars, step=step_bars)
        summaries: list[WalkForwardSummary] = []
        for strategy in strategies:
            version = str(getattr(strategy, "version", strategy_version) or strategy_version)
            oos_trades = 0
            for train, oos in pairs:
                train_result = run_window(self.engine, bars, strategy, train, self.context_bars)
                self.store.record_result(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    regime="ALL", strategy=strategy.name, split="TRAIN",
                    bars=train.size, result=train_result, run_id=run_id,
                    source=source, strategy_version=version,
                )
                oos_result = run_window(self.engine, bars, strategy, oos, self.context_bars)
                offset = max(0, oos.start - self.context_bars)
                by_regime: dict[str, list[BacktestTrade]] = defaultdict(list)
                for trade in oos_result.trade_log:
                    by_regime[self._regime_at_signal(bars, offset + trade.entry_index)].append(trade)
                for regime, trades in by_regime.items():
                    self.store.record_result(
                        symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                        regime=regime, strategy=strategy.name, split="OOS",
                        bars=oos.size, result=_trade_group_result(trades, self.engine.initial_equity),
                        run_id=run_id, source=source, strategy_version=version,
                    )
                oos_trades += oos_result.trades
            summaries.append(WalkForwardSummary(strategy.name, 2 * len(pairs), len(pairs), len(pairs), oos_trades))
        return summaries


__all__ = ["WalkForwardResearch", "WalkForwardSummary", "Window"]
