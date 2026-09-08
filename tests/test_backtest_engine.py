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
