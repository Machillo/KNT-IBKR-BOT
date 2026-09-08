from backtest.engine import BacktestEngine
from market.history import PriceBar
from strategies.library import TrendFollowingStrategy
from strategies.momentum import MomentumStrategy


def make_bars(count=120):
    bars=[]
    price=100.0
    for i in range(count):
        close=price+0.5
        bars.append(PriceBar(str(i), price, close+0.25, price-0.25, close, 1000+i))
        price=close
    return bars


def test_backtest_runs():
    result=BacktestEngine().run(make_bars(), MomentumStrategy(fast=5, slow=15, atr_period=5))
    assert result.initial_equity == 10000
    assert result.trades >= 0
    assert result.final_equity > 0


def test_backtest_accepts_non_momentum_strategy():
    result=BacktestEngine().run(make_bars(), TrendFollowingStrategy())
    assert result.initial_equity == 10000
    assert result.final_equity > 0


def test_position_size_is_capped_by_notional():
    engine = BacktestEngine(initial_equity=10_000, risk_pct=0.01, max_position_pct=0.25)
    qty = engine._position_qty(equity=10_000, entry=100.0, stop=99.99)
    assert qty == 25.0
    assert qty * 100.0 == 2_500.0


def test_position_size_still_respects_stop_risk_when_tighter_than_notional_cap():
    engine = BacktestEngine(initial_equity=10_000, risk_pct=0.01, max_position_pct=1.0)
    qty = engine._position_qty(equity=10_000, entry=100.0, stop=95.0)
    assert qty == 20.0


def test_invalid_max_position_pct_is_rejected():
    for value in (0, -0.1, 1.01):
        try:
            BacktestEngine(max_position_pct=value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"max_position_pct={value} should be rejected")
