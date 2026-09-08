from backtest.engine import BacktestEngine
from market.history import PriceBar
from strategies.momentum import MomentumStrategy


def test_backtest_returns_metrics_without_crashing():
    closes = [100 + i * 0.5 for i in range(100)]
    bars = [PriceBar(str(i), c - 0.2, c + 0.8, c - 0.8, c, 10000) for i, c in enumerate(closes)]
    result = BacktestEngine(initial_equity=10000, risk_pct=0.01).run(
        bars, MomentumStrategy(fast=5, slow=15, atr_period=5)
    )
    assert result.initial_equity == 10000
    assert result.final_equity > 0
    assert result.trades >= 1
    assert 0 <= result.win_rate_pct <= 100
    assert result.max_drawdown_pct >= 0
