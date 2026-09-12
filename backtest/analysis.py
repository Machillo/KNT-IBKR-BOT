from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import csv
from pathlib import Path
from statistics import mean

from backtest.universes import VALIDATION_UNIVERSES


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


@dataclass(frozen=True)
class ContextRobustness:
    context_type: str
    context: str
    strategy: str
    datasets: int
    profitable_pct: float
    positive_excess_pct: float
    avg_return_pct: float
    avg_drawdown_pct: float
    avg_profit_factor: float
    stress_survival_pct: float
    score: float
    status: str


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _score_group(items: list[dict[str, str]]) -> tuple[int, float, float, float, float, float, float, float, str]:
    baseline = [row for row in items if row["scenario"] == "baseline"]
    stress = [row for row in items if row["scenario"] in {"cost_stress_8bps", "cost_stress_15bps"}]
    if not baseline:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "AVOID"

    profitable = mean(_f(row["total_return_pct"]) > 0 for row in baseline)
    positive_excess = mean(_f(row["excess_return_pct"]) > 0 for row in baseline)
    avg_return = mean(_f(row["total_return_pct"]) for row in baseline)
    avg_dd = mean(_f(row["max_drawdown_pct"]) for row in baseline)
    pfs = [_f(row["profit_factor"], 0.0) for row in baseline if row.get("profit_factor") not in {"", None, "None", "inf"}]
    avg_pf = mean(pfs) if pfs else 0.0
    stress_survival = mean(_f(row["total_return_pct"]) > 0 for row in stress) if stress else 0.0
    dataset_count = len({(row["symbol"], row["duration"], row["timeframe"]) for row in baseline})

    score = 100.0 * (
        0.30 * profitable
        + 0.15 * positive_excess
        + 0.25 * stress_survival
        + 0.15 * min(1.0, max(0.0, avg_pf / 1.5))
        + 0.15 * max(0.0, min(1.0, 1.0 - avg_dd / 20.0))
    )
    if dataset_count < 3:
        status = "DEVELOP"
    elif score >= 68 and profitable >= 0.65 and stress_survival >= 0.60 and avg_pf >= 1.05:
        status = "PROMOTE"
    elif score < 42 or profitable < 0.40 or avg_pf < 0.90:
        status = "AVOID"
    else:
        status = "DEVELOP"

    return (
        dataset_count,
        profitable * 100,
        positive_excess * 100,
        avg_return,
        avg_dd,
        avg_pf,
        stress_survival * 100,
        score,
        status,
    )


def analyze_validation_csv(path: str | Path) -> list[StrategyRobustness]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["strategy"]].append(row)

    results: list[StrategyRobustness] = []
    for strategy, items in grouped.items():
        dataset_count, profitable, positive_excess, avg_return, avg_dd, avg_pf, stress, score, status = _score_group(items)
        baseline = [row for row in items if row["scenario"] == "baseline"]
        horizons = len({(row["duration"], row["timeframe"]) for row in baseline})
        results.append(StrategyRobustness(
            strategy=strategy,
            datasets=dataset_count,
            profitable_pct=profitable,
            positive_excess_pct=positive_excess,
            avg_return_pct=avg_return,
            avg_drawdown_pct=avg_dd,
            avg_profit_factor=avg_pf,
            stress_survival_pct=stress,
            horizon_coverage=horizons,
            score=score,
            status=status,
        ))

    return sorted(results, key=lambda row: row.score, reverse=True)


def analyze_contexts(path: str | Path) -> list[ContextRobustness]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    symbol_to_universes: dict[str, set[str]] = defaultdict(set)
    for universe, symbols in VALIDATION_UNIVERSES.items():
        for symbol in symbols:
            symbol_to_universes[symbol].add(universe)

    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strategy = row["strategy"]
        grouped[("timeframe", row["timeframe"], strategy)].append(row)
        grouped[("horizon", f'{row["duration"]}/{row["timeframe"]}', strategy)].append(row)
        grouped[("symbol", row["symbol"], strategy)].append(row)
        for universe in symbol_to_universes.get(row["symbol"], set()):
            grouped[("universe", universe, strategy)].append(row)

    results: list[ContextRobustness] = []
    for (context_type, context, strategy), items in grouped.items():
        dataset_count, profitable, positive_excess, avg_return, avg_dd, avg_pf, stress, score, status = _score_group(items)
        results.append(ContextRobustness(
            context_type=context_type,
            context=context,
            strategy=strategy,
            datasets=dataset_count,
            profitable_pct=profitable,
            positive_excess_pct=positive_excess,
            avg_return_pct=avg_return,
            avg_drawdown_pct=avg_dd,
            avg_profit_factor=avg_pf,
            stress_survival_pct=stress,
            score=score,
            status=status,
        ))
    return sorted(results, key=lambda row: (row.context_type, row.context, -row.score))
