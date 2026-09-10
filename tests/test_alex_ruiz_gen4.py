from research.alex_ruiz_gen4 import Gen4Candidate, admit_candidates, simulate_equal_risk_portfolio


def _c(trades=20, monthly=0.2, positive=60.0, dd=2.0):
    return Gen4Candidate("X", "intraday_1y", "s", monthly, dd, positive, trades)


def test_admission_requires_enough_untouched_test_evidence():
    rows = [_c(), _c(trades=4), _c(monthly=-0.1), _c(positive=40), _c(dd=12)]
    assert admit_candidates(rows) == [rows[0]]


def test_shared_capital_does_not_sum_full_account_returns():
    series = {
        "a": {"2026-01": 10.0, "2026-02": 10.0},
        "b": {"2026-01": 10.0, "2026-02": 10.0},
    }
    result = simulate_equal_risk_portfolio(series, max_strategy_weight=0.20, max_gross_weight=1.0)
    # Two sleeves at 20% each => portfolio earns 4%, not 20%, in each month.
    assert 3.99 < result.compounded_monthly_pct < 4.01
    assert result.positive_month_rate_pct == 100.0


def test_gross_cap_divides_weight_across_many_active_sleeves():
    series = {str(i): {"2026-01": 10.0, "2026-02": 10.0} for i in range(10)}
    result = simulate_equal_risk_portfolio(series, max_strategy_weight=0.20, max_gross_weight=1.0)
    assert 9.99 < result.compounded_monthly_pct < 10.01
