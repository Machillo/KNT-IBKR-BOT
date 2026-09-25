"""Regression tests for the simulator fixes from the audit (gap stops, costs, qty, Sharpe, windows)."""
from datetime import datetime, timedelta

import pytest

from backtest.costs import BASELINE, CostModel
from backtest.engine import BacktestEngine
from backtest.metrics import period_returns, sharpe, sortino
from market.history import PriceBar
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2025, 1, 6, 10, 0)


def day_bars(ohlc):
    """One bar per calendar day so daily metrics are well defined."""
    return [PriceBar(T0 + timedelta(days=i), o, h, l, c, 1000) for i, (o, h, l, c) in enumerate(ohlc)]


class OnceAt:
    """Emit one signal when len(bars) == at, then stay flat."""

    warmup = 1

    def __init__(self, side=SignalSide.LONG, at=2, entry=100.0, stop=95.0, target=120.0):
        self.side, self.at, self.levels = side, at, (entry, stop, target)

    def evaluate(self, bars):
        if len(bars) == self.at:
            entry, stop, target = self.levels
            return StrategySignal(self.side, 90, entry, stop, target, "once")
        return StrategySignal(SignalSide.FLAT, 0, None, None, None, "flat")


FLAT = (100, 100, 100, 100)


def engine(**kw):
    kw.setdefault("cost_model", CostModel.zero())
    kw.setdefault("risk_pct", 0.01)
    kw.setdefault("max_position_pct", 1.0)
    return BacktestEngine(initial_equity=10_000, **kw)


def test_long_gap_through_stop_fills_at_open_not_stop():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (80, 81, 79, 80), (80, 80, 80, 80)])
    trade = engine().run(bars, OnceAt()).trade_log[0]
    assert (trade.entry, trade.exit, trade.reason) == (100.0, 80.0, "stop_gap")


def test_short_gap_through_stop_fills_at_open():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (130, 131, 129, 130), (130, 130, 130, 130)])
    strat = OnceAt(SignalSide.SHORT, entry=100, stop=105, target=80)
    trade = engine().run(bars, strat).trade_log[0]
    assert (trade.exit, trade.reason) == (130.0, "stop_gap")
    assert trade.pnl < 0


def test_intrabar_stop_without_gap_still_fills_at_stop():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (99, 99.5, 90, 92), FLAT])
    trade = engine().run(bars, OnceAt()).trade_log[0]
    assert (trade.exit, trade.reason) == (95.0, "stop")


def test_stop_wins_when_stop_and_target_touch_same_bar():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (100, 125, 90, 100), FLAT])
    assert engine().run(bars, OnceAt()).trade_log[0].reason == "stop"


def test_target_is_a_limit_fill_without_slippage():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (110, 121, 109, 120), FLAT])
    costs = CostModel(commission_per_share=0, commission_min=0, half_spread_bps=10, slippage_bps=10, sell_fee_bps=0)
    trade = engine(cost_model=costs).run(bars, OnceAt()).trade_log[0]
    assert trade.reason == "target" and trade.exit == 120.0
    assert trade.entry == pytest.approx(100 * 1.002)


def test_ibkr_commission_minimum_and_cap():
    c = BASELINE
    assert c.commission(10, 100) == pytest.approx(1.00)          # 10 x 0.005 = 0.05 -> min 1.00
    assert c.commission(1000, 100) == pytest.approx(5.00)        # per share
    assert c.commission(1000, 0.10) == pytest.approx(1.00)       # 1 % cap of 100 USD notional
    assert c.commission(0, 100) == 0.0


def test_costs_are_reported_separately_and_reduce_pnl():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (110, 121, 109, 120), FLAT])
    free = engine().run(bars, OnceAt())
    paid = engine(cost_model=BASELINE).run(bars, OnceAt())
    t_free, t_paid = free.trade_log[0], paid.trade_log[0]
    assert t_paid.pnl < t_free.pnl
    assert paid.total_commission >= 2.0            # two orders at the 1 USD minimum
    assert paid.total_spread_slippage > 0
    assert t_paid.commission >= 2.0 and t_paid.spread_slippage > 0
    assert paid.final_equity == pytest.approx(10_000 + t_paid.pnl)


def test_small_capital_pays_proportionally_more():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (110, 121, 109, 120), FLAT])
    small = BacktestEngine(initial_equity=2_000, risk_pct=0.01, max_position_pct=1.0, cost_model=BASELINE)
    large = BacktestEngine(initial_equity=200_000, risk_pct=0.01, max_position_pct=1.0, cost_model=BASELINE)
    r_small = small.run(bars, OnceAt()).trade_log[0]
    r_large = large.run(bars, OnceAt()).trade_log[0]
    assert r_small.commission / (r_small.quantity * 100) > r_large.commission / (r_large.quantity * 100)


def test_integer_shares_and_skip_when_budget_below_one_share():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (110, 121, 109, 120), FLAT])
    result = engine().run(bars, OnceAt())
    assert result.trade_log[0].quantity == 20.0    # 100 risk / 5 per share
    tiny = BacktestEngine(initial_equity=300, risk_pct=0.01, max_position_pct=1.0, cost_model=CostModel.zero())
    tiny_result = tiny.run(bars, OnceAt())          # 3 USD risk / 5 per share < 1 share
    assert tiny_result.trades == 0 and tiny_result.skipped_min_quantity == 1


def test_fractional_mode_is_explicit():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (110, 121, 109, 120), FLAT])
    e = BacktestEngine(initial_equity=300, risk_pct=0.01, max_position_pct=1.0,
                       cost_model=CostModel.zero(), quantity_step=None)
    assert e.run(bars, OnceAt()).trade_log[0].quantity == pytest.approx(0.6)


def test_daily_sharpe_does_not_scale_with_trade_count():
    returns = [0.01, -0.005, 0.004, 0.002, -0.001] * 20
    assert sharpe(returns) == pytest.approx(sharpe(returns * 4), rel=0.05)
    assert sharpe([0.001] * 10) is None           # zero variance -> undefined, not infinite
    assert sortino([0.01, 0.02]) is None          # no downside


def test_period_returns_start_from_initial_equity():
    assert period_returns([110, 99], 100) == pytest.approx([0.10, -0.10])


def test_result_sharpe_is_daily_and_annualized():
    ohlc = [FLAT, FLAT] + [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(30)]
    result = engine().run(day_bars(ohlc), OnceAt(target=1_000))
    assert result.sharpe is not None
    assert result.trade_sharpe is None or result.trade_sharpe != result.sharpe


def test_trade_start_blocks_earlier_entries_but_allows_warmup_reads():
    class NeedsHistory:
        warmup = 5
        seen = []

        def evaluate(self, bars):
            self.seen.append(len(bars))
            return StrategySignal(SignalSide.LONG, 90, 100.0, 95.0, 120.0, "always")

    bars = day_bars([FLAT] * 12)
    strat = NeedsHistory()
    result = engine().run(bars, strat, trade_start=8)
    assert all(t.entry_index >= 8 for t in result.trade_log)
    assert min(strat.seen) == 8                    # first evaluation used 8 prior bars (warmup)
    assert result.equity_curve[0].time == bars[8].time


def test_trade_start_window_without_room_has_no_trades():
    bars = day_bars([FLAT] * 5)
    assert engine().run(bars, OnceAt(at=2), trade_start=5).trades == 0


def test_short_can_be_disabled():
    bars = day_bars([FLAT, FLAT, (100, 101, 99, 100), (90, 91, 79, 80), FLAT])
    strat = OnceAt(SignalSide.SHORT, entry=100, stop=105, target=80)
    assert engine(allow_short=False).run(bars, strat).trades == 0


def test_legacy_bps_arguments_still_supported_and_exclusive():
    e = BacktestEngine(commission_bps=1, slippage_bps=2)
    assert e.costs.name == "legacy_bps"
    with pytest.raises(ValueError):
        BacktestEngine(commission_bps=1, cost_model=BASELINE)
