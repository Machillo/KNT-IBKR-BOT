import csv

from backtest.analysis import analyze_validation_csv


def test_global_analysis_promotes_robust_strategy(tmp_path):
    path = tmp_path / "all.csv"
    fields = [
        "symbol", "timeframe", "duration", "strategy", "scenario", "bars",
        "initial_equity", "final_equity", "total_return_pct", "max_drawdown_pct",
        "trades", "win_rate_pct", "profit_factor", "sharpe",
        "buy_hold_return_pct", "excess_return_pct",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for i in range(6):
            for scenario in ("baseline", "cost_stress_8bps", "cost_stress_15bps"):
                writer.writerow({
                    "symbol": f"S{i}", "timeframe": "1 hour", "duration": "1 Y",
                    "strategy": "robust_v1", "scenario": scenario, "bars": 1000,
                    "initial_equity": 10000, "final_equity": 11000,
                    "total_return_pct": 10, "max_drawdown_pct": 5, "trades": 40,
                    "win_rate_pct": 55, "profit_factor": 1.8, "sharpe": 1.0,
                    "buy_hold_return_pct": 5, "excess_return_pct": 5,
                })
    result = analyze_validation_csv(path)
    assert result[0].strategy == "robust_v1"
    assert result[0].status == "PROMOTE"
    assert result[0].score >= 68
