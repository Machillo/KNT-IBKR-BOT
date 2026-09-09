from __future__ import annotations

import argparse
from pathlib import Path

from backtest.analysis import analyze_validation_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze KNT validation results across symbols/horizons/scenarios")
    parser.add_argument("--input", default="reports/backtests/ALL_RESULTS.csv")
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        raise FileNotFoundError(f"Validation report not found: {path}")

    rows = analyze_validation_csv(path)
    print("GLOBAL STRATEGY ROBUSTNESS")
    for row in rows:
        print(
            f"{row.status:8} {row.strategy:28} score={row.score:5.1f} "
            f"datasets={row.datasets:3d} profitable={row.profitable_pct:5.1f}% "
            f"stress={row.stress_survival_pct:5.1f}% excess={row.positive_excess_pct:5.1f}% "
            f"ret={row.avg_return_pct:7.2f}% dd={row.avg_drawdown_pct:6.2f}% pf={row.avg_profit_factor:5.2f}"
        )


if __name__ == "__main__":
    main()
