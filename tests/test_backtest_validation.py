from datetime import datetime, timedelta
from pathlib import Path

from backtest.validation import (
    ValidationScenario,
    buy_hold_return_pct,
    robustness_score,
    run_validation_matrix,
    write_validation_report,
)
from market.history import PriceBar
from strategies.momentum import MomentumStrategy


def bars(count=100):
    start = datetime(2026, 1, 1)
    result = []
    price = 100.0
    for i in range(count):
        price += 0.5
        result.append(PriceBar(start + timedelta(hours=i), price - 0.2, price + 1, price - 1, price, 1000))
    return result


def test_buy_hold_return_uses_first_and_last_close():
    data = bars(10)
    expected = (data[-1].close / data[0].close - 1) * 100
    assert buy_hold_return_pct(data) == expected


def test_validation_matrix_runs_strategy_across_scenarios():
    scenarios = (
        ValidationScenario("a", 0.01, 1, 2, 0.25),
        ValidationScenario("b", 0.005, 3, 5, 0.10),
    )
    rows = run_validation_matrix(
        symbol="TEST",
        timeframe="1 hour",
        duration="1 Y",
        bars=bars(120),
        strategies=[MomentumStrategy()],
        scenarios=scenarios,
    )
    assert len(rows) == 2
    assert {row.scenario for row in rows} == {"a", "b"}
    assert all(row.symbol == "TEST" for row in rows)


def test_robustness_score_is_bounded():
    rows = run_validation_matrix(
        symbol="TEST",
        timeframe="1 hour",
        duration="1 Y",
        bars=bars(120),
        strategies=[MomentumStrategy()],
    )
    assert 0 <= robustness_score(rows) <= 100


def test_validation_report_writes_csv_and_json(tmp_path: Path):
    rows = run_validation_matrix(
        symbol="TEST",
        timeframe="1 hour",
        duration="1 Y",
        bars=bars(120),
        strategies=[MomentumStrategy()],
    )
    csv_path, json_path = write_validation_report(rows, tmp_path, "test")
    assert csv_path.exists()
    assert json_path.exists()
    assert "buy_hold_return_pct" in csv_path.read_text(encoding="utf-8")
