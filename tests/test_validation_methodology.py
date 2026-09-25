"""Methodology guards: no future data, OOS-only evidence, warmup that works in short windows."""
from datetime import datetime, timedelta
import sqlite3

import pytest

from backtest.costs import CostModel
from backtest.engine import BacktestEngine
from market.history import PriceBar
from research.performance import StrategyPerformanceStore
from research.splits import Window, chronological_split, rolling_windows, run_window
from research.walkforward import WalkForwardResearch
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2024, 1, 2, 10)


def zigzag(n=900):
    bars, price = [], 100.0
    for i in range(n):
        step = 0.6 if (i // 15) % 2 == 0 else -0.5
        o, c = price, price + step
        bars.append(PriceBar(T0 + timedelta(hours=i), o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000))
        price = c
    return bars


class Spy:
    """Records how much history it was shown; trades a simple breakout-like rule."""

    name = "spy_v1"

    def __init__(self, warmup=80):
        self.warmup = warmup
        self.max_len = 0
        self.last_times = []

    def evaluate(self, bars):
        self.max_len = max(self.max_len, len(bars))
        self.last_times.append(bars[-1].time)
        if len(bars) < self.warmup:
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "warmup")
        price = bars[-1].close
        if bars[-1].close > bars[-2].close:
            return StrategySignal(SignalSide.LONG, 70, price, price - 1.0, price + 1.5, "up")
        return StrategySignal(SignalSide.FLAT, 0, None, None, None, "flat")


def engine():
    return BacktestEngine(cost_model=CostModel.zero(), max_position_pct=1.0)


def test_chronological_split_is_ordered_and_disjoint():
    s = chronological_split(1000)
    assert (s.train.start, s.train.end, s.validation.end, s.holdout.end) == (0, 600, 800, 1000)
    assert s.train.end == s.validation.start and s.validation.end == s.holdout.start
    with pytest.raises(ValueError):
        chronological_split(1000, train=0.9, validation=0.2)


def test_run_window_never_shows_bars_after_window_end():
    bars = zigzag(500)
    strat = Spy()
    window = Window("HOLDOUT", 300, 380)
    run_window(engine(), bars, strat, window, context_bars=200)
    assert max(strat.last_times) <= bars[window.end - 1].time


def test_run_window_counts_only_entries_inside_window():
    bars = zigzag(500)
    window = Window("OOS", 300, 380)
    result = run_window(engine(), bars, Spy(), window, context_bars=200)
    lo = window.start - 200
    assert result.trades > 0
    assert all(window.start <= lo + t.entry_index < window.end for t in result.trade_log)


def test_short_oos_window_still_trades_when_warmup_exceeds_window():
    # Old walk-forward ran an 80-bar OOS slice with no prior context: a strategy with
    # warmup=80 could never trade there. With context it can.
    bars = zigzag(500)
    window = Window("OOS", 400, 480)
    without_context = run_window(engine(), bars, Spy(warmup=80), window, context_bars=0)
    with_context = run_window(engine(), bars, Spy(warmup=80), window, context_bars=200)
    assert without_context.trades == 0
    assert with_context.trades > 0


def test_rolling_oos_windows_do_not_overlap():
    pairs = rolling_windows(1000, train=240, test=80, step=80)
    tests = [oos for _, oos in pairs]
    assert all(a.end <= b.start for a, b in zip(tests, tests[1:]))
    assert all(train.end == oos.start for train, oos in pairs)


def test_walkforward_stores_train_as_diagnostic_and_oos_by_signal_regime(tmp_path):
    store = StrategyPerformanceStore(tmp_path / "wf.db")
    run = store.begin_research_run(symbol="ZZ", asset_class="STK", timeframe="1 hour", bars=900)
    summaries = WalkForwardResearch(store, engine(), context_bars=200).evaluate(
        symbol="ZZ", asset_class="STK", timeframe="1 hour", bars=zigzag(900),
        strategies=[Spy()], run_id=run,
    )
    store.finish_research_run(run)
    assert summaries[0].oos_trades > 0
    with sqlite3.connect(store.path) as conn:
        train_regimes = {r[0] for r in conn.execute(
            "SELECT DISTINCT regime FROM strategy_performance WHERE split='TRAIN'")}
        oos_regimes = {r[0] for r in conn.execute(
            "SELECT DISTINCT regime FROM strategy_performance WHERE split='OOS'")}
        versions = {r[0] for r in conn.execute("SELECT DISTINCT engine_version FROM strategy_performance")}
    assert train_regimes == {"ALL"}
    assert oos_regimes and "ALL" not in oos_regimes
    assert versions == {2}
    for regime in oos_regimes:
        ev = store.evidence(symbol="ZZ", asset_class="STK", timeframe="1 hour", regime=regime, strategy="spy_v1")
        assert ev is not None and ev.oos_samples == ev.samples
