from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import csv
from pathlib import Path
from statistics import mean


@dataclass(frozen=True)
class StrategyRobustness:
    strategy: str
    datasets: int
    profitable_pct: float
    positive_excess_pct: float
    avg_return_pct: float
    avg_drawdown_pct: float
    avg_profit_factor: float
    stress_survival_pct: float
    horizon_coverage: int
    score: float
    status: str


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def analyze_validation_csv(path: str | Path) -> list[StrategyRobustness]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["strategy"]].append(row)

    results: list[StrategyRobustness] = []
    for strategy, items in grouped.items():
        baseline = [row for row in items if row["scenario"] == "baseline"]
        stress = [row for row in items if row["scenario"] in {"cost_stress_8bps", "cost_stress_15bps"}]
        if not baseline:
            continue
        profitable = mean(_f(row["total_return_pct"]) > 0 for row in baseline)
        positive_excess = mean(_f(row["excess_return_pct"]) > 0 for row in baseline)
        avg_return = mean(_f(row["total_return_pct"]) for row in baseline)
        avg_dd = mean(_f(row["max_drawdown_pct"]) for row in baseline)
        pfs = [_f(row["profit_factor"], 0.0) for row in baseline if row.get("profit_factor") not in {"", None, "None", "inf"}]
        avg_pf = mean(pfs) if pfs else 0.0
        stress_survival = mean(_f(row["total_return_pct"]) > 0 for row in stress) if stress else 0.0
        horizons = len({(row["duration"], row["timeframe"]) for row in baseline})
        dataset_count = len({(row["symbol"], row["duration"], row["timeframe"]) for row in baseline})

        score = 100.0 * (
            0.30 * profitable
            + 0.15 * positive_excess
            + 0.25 * stress_survival
            + 0.15 * min(1.0, max(0.0, avg_pf / 1.5))
            + 0.15 * max(0.0, min(1.0, 1.0 - avg_dd / 20.0))
        )
        if dataset_count < 6:
            status = "DEVELOP"
        elif score >= 68 and profitable >= 0.65 and stress_survival >= 0.60:
            status = "PROMOTE"
        elif score < 42 or profitable < 0.40:
            status = "AVOID"
        else:
            status = "DEVELOP"

        results.append(StrategyRobustness(
            strategy=strategy,
            datasets=dataset_count,
            profitable_pct=profitable * 100,
            positive_excess_pct=positive_excess * 100,
            avg_return_pct=avg_return,
            avg_drawdown_pct=avg_dd,
            avg_profit_factor=avg_pf,
            stress_survival_pct=stress_survival * 100,
            horizon_coverage=horizons,
            score=score,
            status=status,
        ))

    return sorted(results, key=lambda row: row.score, reverse=True)
