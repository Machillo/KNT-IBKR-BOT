from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import csv
import json

from backtest.engine import BacktestEngine, BacktestResult
from market.history import PriceBar


@dataclass(frozen=True)
class ValidationScenario:
    name: str
    risk_pct: float
    commission_bps: float
    slippage_bps: float
    max_position_pct: float


@dataclass(frozen=True)
class ValidationRow:
    symbol: str
    timeframe: str
    duration: str
    strategy: str
    scenario: str
    bars: int
    initial_equity: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float | None
    sharpe: float | None
    buy_hold_return_pct: float
    excess_return_pct: float


DEFAULT_SCENARIOS = (
    ValidationScenario("baseline", 0.01, 1.0, 2.0, 0.25),
    ValidationScenario("cost_stress_8bps", 0.01, 3.0, 5.0, 0.25),
    ValidationScenario("cost_stress_15bps", 0.01, 5.0, 10.0, 0.25),
    ValidationScenario("risk_half_pct", 0.005, 1.0, 2.0, 0.25),
    ValidationScenario("risk_two_pct", 0.02, 1.0, 2.0, 0.25),
    ValidationScenario("position_cap_10pct", 0.01, 1.0, 2.0, 0.10),
)


def buy_hold_return_pct(bars: list[PriceBar]) -> float:
    if len(bars) < 2 or bars[0].close <= 0:
        return 0.0
    return (bars[-1].close / bars[0].close - 1.0) * 100.0


def run_validation_matrix(
    *,
    symbol: str,
    timeframe: str,
    duration: str,
    bars: list[PriceBar],
    strategies: list[object],
    initial_equity: float = 10_000.0,
    scenarios: tuple[ValidationScenario, ...] = DEFAULT_SCENARIOS,
) -> list[ValidationRow]:
    benchmark = buy_hold_return_pct(bars)
    rows: list[ValidationRow] = []
    for strategy in strategies:
        for scenario in scenarios:
            engine = BacktestEngine(
                initial_equity=initial_equity,
                risk_pct=scenario.risk_pct,
                commission_bps=scenario.commission_bps,
                slippage_bps=scenario.slippage_bps,
                max_position_pct=scenario.max_position_pct,
            )
            result: BacktestResult = engine.run(bars, strategy)
            rows.append(ValidationRow(
                symbol=symbol,
                timeframe=timeframe,
                duration=duration,
                strategy=str(strategy.name),
                scenario=scenario.name,
                bars=len(bars),
                initial_equity=result.initial_equity,
                final_equity=result.final_equity,
                total_return_pct=result.total_return_pct,
                max_drawdown_pct=result.max_drawdown_pct,
                trades=result.trades,
                win_rate_pct=result.win_rate_pct,
                profit_factor=result.profit_factor,
                sharpe=result.sharpe,
                buy_hold_return_pct=benchmark,
                excess_return_pct=result.total_return_pct - benchmark,
            ))
    return rows


def robustness_score(rows: list[ValidationRow]) -> float:
    """Simple 0-100 robustness score across scenarios, intentionally not a profit claim."""
    if not rows:
        return 0.0
    profitable = sum(row.total_return_pct > 0 for row in rows) / len(rows)
    controlled_dd = sum(row.max_drawdown_pct <= 10 for row in rows) / len(rows)
    enough_trades = sum(row.trades >= 10 for row in rows) / len(rows)
    return 100.0 * (0.45 * profitable + 0.35 * controlled_dd + 0.20 * enough_trades)


def write_validation_report(rows: list[ValidationRow], output_dir: str | Path, stem: str) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / f"{stem}.csv"
    json_path = output / f"{stem}.json"
    fields = list(ValidationRow.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(row) for row in rows], handle, indent=2)
    return csv_path, json_path
