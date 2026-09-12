from datetime import datetime

from backtest.engine import BacktestEquityPoint, BacktestResult
from backtest.monthly_target import monthly_returns, summarize_months


def result_with_curve():
    return BacktestResult(
        initial_equity=100.0,
        final_equity=121.0,
        total_return_pct=21.0,
        max_drawdown_pct=2.0,
        trades=4,
        wins=3,
        losses=1,
        win_rate_pct=75.0,
        profit_factor=2.0,
        sharpe=1.0,
        trade_log=(),
        equity_curve=(
            BacktestEquityPoint(datetime(2026, 1, 5), 100.0, 100.0, 0.0),
            BacktestEquityPoint(datetime(2026, 1, 30), 110.0, 110.0, 0.0),
            BacktestEquityPoint(datetime(2026, 2, 27), 121.0, 121.0, 0.0),
        ),
    )


def test_monthly_returns_use_mark_to_market_month_ends():
    values = monthly_returns(result_with_curve())
    assert len(values) == 2
    assert round(values[0], 6) == 10.0
    assert round(values[1], 6) == 10.0


def test_monthly_summary_measures_target_hit_rate_and_compounding():
    stats = summarize_months(result_with_curve(), target_pct=5.0)
    assert stats.months == 2
    assert round(stats.compounded_monthly_pct, 6) == 10.0
    assert stats.positive_month_rate_pct == 100.0
    assert stats.target_hit_rate_pct == 100.0
    assert stats.worst_month_pct > 9.99
