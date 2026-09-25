"""Regression tests requested by the independent quant-methodology review."""
from datetime import datetime, timedelta
from math import sqrt

import pytest

from backtest.costs import BASELINE, CostModel
from backtest.engine import BacktestEngine, BacktestResult
from backtest.metrics import period_returns, sharpe
from market.history import PriceBar
from market.regime import MarketRegime, RegimeSnapshot
from research.performance import StrategyPerformanceStore
from research.splits import rolling_windows
from research.walkforward import WalkForwardResearch
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2024, 1, 2, 10)


def hourly(n=700):
    bars, price = [], 100.0
    for i in range(n):
        step = 0.6 if (i // 15) % 2 == 0 else -0.5
        o, c = price, price + step
        bars.append(PriceBar(T0 + timedelta(hours=i), o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000))
        price = c
    return bars


class UpStrategy:
    name = "up_v1"
    warmup = 30

    def evaluate(self, bars):
        p = bars[-1].close
        if bars[-1].close > bars[-2].close:
            return StrategySignal(SignalSide.LONG, 70, p, p - 1.0, p + 1.5, "up")
        return StrategySignal(SignalSide.FLAT, 0, None, None, None, "flat")


class SpyDetector:
    """Records the last bar each regime label was computed from."""

    def __init__(self):
        self.last_seen = []

    def evaluate(self, bars):
        self.last_seen.append(bars[-1].time)
        return RegimeSnapshot(MarketRegime.RANGE, 0, 0, "spy")


def test_regime_label_uses_only_bars_before_the_entry_bar(tmp_path):
    bars = hourly()
    store = StrategyPerformanceStore(tmp_path / "wf.db")
    wf = WalkForwardResearch(store, BacktestEngine(cost_model=CostModel.zero(), max_position_pct=1.0),
                             context_bars=200)
    spy = SpyDetector()
    wf.regime_detector = spy
    run = store.begin_research_run(symbol="Z", asset_class="STK", timeframe="1 hour", bars=len(bars))
    wf.evaluate(symbol="Z", asset_class="STK", timeframe="1 hour", bars=bars, strategies=[UpStrategy()], run_id=run)
    entry_times = []
    # Recompute OOS entries the same way to compare each label with its entry bar.
    from research.splits import run_window
    for _, oos in rolling_windows(len(bars), train=240, test=80, step=80):
        result = run_window(wf.engine, bars, UpStrategy(), oos, 200)
        lo = max(0, oos.start - 200)
        entry_times += [bars[lo + t.entry_index].time for t in result.trade_log]
    assert len(spy.last_seen) == len(entry_times) > 0
    assert all(seen < entry for seen, entry in zip(spy.last_seen, entry_times))


def test_daily_sharpe_matches_hand_computation():
    equity = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]
    rets = period_returns(equity[1:], equity[0])
    m = sum(rets) / len(rets)
    sd = sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    assert sharpe(rets) == pytest.approx(m / sd * sqrt(252))


def test_short_borrow_cost_is_charged_by_days_held():
    assert BASELINE.borrow_cost(10_000, 36) == pytest.approx(10_000 * 0.01 * 36 / 360)
    days = [PriceBar(T0 + timedelta(days=i), *p, 1000) for i, p in enumerate(
        [(100, 100, 100, 100)] * 2 + [(100, 101, 99, 100)] + [(99, 100, 98, 99)] * 30 + [(80, 81, 79, 80)])]

    class ShortOnce:
        warmup = 1

        def evaluate(self, bars):
            if len(bars) == 2:
                return StrategySignal(SignalSide.SHORT, 90, 100, 110, 80, "s")
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "f")

    free = CostModel.zero()
    with_borrow = CostModel("b", 0, 0, 0, 0, 0, 0, 0, short_borrow_annual_pct=10.0)
    a = BacktestEngine(cost_model=free, max_position_pct=1.0).run(days, ShortOnce()).trade_log[0]
    b = BacktestEngine(cost_model=with_borrow, max_position_pct=1.0).run(days, ShortOnce()).trade_log[0]
    assert b.pnl < a.pnl
    assert (a.pnl - b.pnl) == pytest.approx(a.entry * a.quantity * 0.10 * 31 / 360, rel=0.01)


def test_mark_to_market_includes_full_exit_cost():
    bars = [PriceBar(T0 + timedelta(days=i), *p, 1000) for i, p in enumerate(
        [(100, 100, 100, 100)] * 2 + [(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])]

    class LongOnce:
        warmup = 1

        def evaluate(self, bars):
            if len(bars) == 2:
                return StrategySignal(SignalSide.LONG, 90, 100, 90, 150, "l")
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "f")

    result = BacktestEngine(cost_model=BASELINE, max_position_pct=1.0).run(bars, LongOnce())
    open_point = result.equity_curve[2]  # entry bar: position open, close unchanged at 100
    qty = result.trade_log[0].quantity
    expected_exit = BASELINE.estimated_exit_cost(qty, 100.0, long=True)
    assert open_point.equity == pytest.approx(open_point.realized_equity + (100.0 - result.trade_log[0].entry) * qty - expected_exit)


def test_overlapping_oos_windows_are_refused():
    with pytest.raises(ValueError):
        rolling_windows(1000, train=240, test=80, step=40)


def _result(ret, trades):
    return BacktestResult(10000, 10000 * (1 + ret / 100), ret, 1.0, trades, trades, 0, 100.0, 2.0, None, ())


def test_evidence_does_not_resurface_older_runs_for_a_regime(tmp_path):
    store = StrategyPerformanceStore(tmp_path / "p.db")
    old = store.begin_research_run(symbol="S", asset_class="STK", timeframe="1 hour", bars=1)
    store.record_result(symbol="S", asset_class="STK", timeframe="1 hour", regime="RANGE", strategy="x",
                        split="OOS", bars=80, result=_result(9.0, 40), run_id=old)
    store.finish_research_run(old)
    new = store.begin_research_run(symbol="S", asset_class="STK", timeframe="1 hour", bars=1)
    store.record_result(symbol="S", asset_class="STK", timeframe="1 hour", regime="TRENDING", strategy="x",
                        split="OOS", bars=80, result=_result(-2.0, 30), run_id=new)
    store.finish_research_run(new)
    assert store.evidence(symbol="S", asset_class="STK", timeframe="1 hour", regime="RANGE", strategy="x") is None
    assert store.evidence(symbol="S", asset_class="STK", timeframe="1 hour", regime="TRENDING", strategy="x") is not None


def test_gen4_run_admits_through_validation_loader(tmp_path, monkeypatch):
    import run_confluence_gen4 as gen4

    called = {}

    def fake_loader(path):
        called["loader"] = path
        return []

    monkeypatch.setattr(gen4, "load_validation_candidates", fake_loader)
    src = tmp_path / "g3.csv"
    src.write_text("x\n")
    gen4.run(gen3_csv=str(src), cache_dir=str(tmp_path), output_dir=str(tmp_path / "out"), min_trades=12)
    assert called["loader"] == src
