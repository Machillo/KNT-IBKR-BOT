from datetime import datetime, timedelta
from random import Random

from backtest.costs import CostModel
from market.history import PriceBar
from research.pipeline_backtest import PipelineBacktest, PipelineConfig, time_split
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2024, 1, 2, 10)


def walk(seed, n=400):
    rng = Random(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0.0005, 0.01)))
        bars.append(PriceBar(T0 + timedelta(hours=i), o, max(o, c) * 1.003, min(o, c) * 0.997, c, 1e6))
        price = c
    return bars


class Spy:
    """Always long; records the latest bar time it was shown."""

    warmup = 30

    def __init__(self, name="spy_v1", side=SignalSide.LONG):
        self.name, self.side, self.seen = name, side, []

    def evaluate(self, bars):
        self.seen.append(bars[-1].time)
        if len(bars) < self.warmup:
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "warmup")
        p = bars[-1].close
        if self.side == SignalSide.LONG:
            return StrategySignal(self.side, 99, p, p * 0.97, p * 1.03, "spy")
        return StrategySignal(self.side, 99, p, p * 1.03, p * 0.97, "spy")


def pipeline(data, spy=None, **cfg):
    cfg.setdefault("cost_model", CostModel.zero())
    cfg.setdefault("pause_high_volatility", False)
    bt = PipelineBacktest(data, PipelineConfig(**cfg))
    bt.selector.strategies = [spy or Spy()]
    return bt


def test_no_bar_at_or_after_end_is_ever_read():
    data = {"A": walk(1), "B": walk(2)}
    spy = Spy()
    bt = pipeline(data, spy)
    end = data["A"][300].time
    bt.run(end=end)
    assert max(spy.seen) < end


def test_entries_only_inside_window():
    data = {"A": walk(1), "B": walk(2)}
    bt = pipeline(data)
    start, end = data["A"][200].time, data["A"][350].time
    result = bt.run(start=start, end=end)
    assert result.trades > 0
    assert all(start <= t.entry_time < end for t in result.trade_log)
    assert result.equity_curve[0][0] >= start


def test_long_only_by_default_matches_live_executor():
    data = {"A": walk(1)}
    result = pipeline(data, Spy(side=SignalSide.SHORT)).run()
    assert result.trades == 0
    allowed = pipeline(data, Spy(side=SignalSide.SHORT), allow_short=True).run()
    assert allowed.trades > 0 and all(t.side == "SHORT" for t in allowed.trade_log)


def test_gross_exposure_and_position_caps_hold():
    data = {f"S{i}": walk(10 + i) for i in range(15)}
    bt = pipeline(data, max_correlation=1.0)
    result = bt.run()
    assert result.trades > 0
    # rebuild open notional over time from the trade log
    events = []
    for t in result.trade_log:
        notional = t.entry * t.quantity
        assert notional <= 0.10 * 100_000 * 1.5  # per-position cap relative to (growing) equity
        events.append((t.entry_time, notional))
        events.append((t.exit_time, -notional))
    events.sort(key=lambda e: (e[0], e[1]))
    open_notional = peak = 0.0
    for _, delta in events:
        open_notional += delta
        peak = max(peak, open_notional)
    assert peak <= 0.80 * max(v for _, v in result.equity_curve) + 1e-6
    assert result.rejected_by_portfolio > 0


def test_time_split_orders_boundaries():
    timeline = [T0 + timedelta(hours=i) for i in range(100)]
    v, h = time_split(timeline)
    assert timeline[0] < v < h < timeline[-1]
