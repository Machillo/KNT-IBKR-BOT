from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
import csv
import json

from backtest.engine import BacktestEngine, BacktestResult
from backtest.validation import DEFAULT_SCENARIOS, ValidationScenario
from market.history import PriceBar


@dataclass(frozen=True)
class MonthlyStats:
    months: int
    compounded_monthly_pct: float
    mean_monthly_pct: float
    median_monthly_pct: float
    positive_month_rate_pct: float
    target_hit_rate_pct: float
    worst_month_pct: float
    best_month_pct: float


@dataclass(frozen=True)
class MonthlyTargetRow:
    symbol: str
    profile: str
    timeframe: str
    duration: str
    strategy: str
    scenario: str
    bars: int
    months: int
    total_return_pct: float
    compounded_monthly_pct: float
    mean_monthly_pct: float
    median_monthly_pct: float
    positive_month_rate_pct: float
    target_5pct_hit_rate_pct: float
    worst_month_pct: float
    best_month_pct: float
    max_drawdown_pct: float
    trades: int
    profit_factor: float | None
    oos_bars: int
    oos_months: int
    oos_return_pct: float
    oos_compounded_monthly_pct: float
    oos_target_5pct_hit_rate_pct: float
    oos_max_drawdown_pct: float
    candidate_5pct: bool


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    candidates = (
        "%Y%m%d",
        "%Y-%m-%d",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def monthly_returns(result: BacktestResult) -> list[float]:
    """Return mark-to-market calendar-month returns in percent.

    Month ends are derived from the equity curve, so open positions are included rather
    than silently disappearing until they are realized.
    """
    if not result.equity_curve:
        return []
    month_ends: list[tuple[tuple[int, int], float]] = []
    current_key = None
    current_equity = None
    for point in result.equity_curve:
        dt = _as_datetime(point.time)
        if dt is None:
            continue
        key = (dt.year, dt.month)
        if current_key is None:
            current_key = key
        if key != current_key:
            if current_equity is not None:
                month_ends.append((current_key, current_equity))
            current_key = key
        current_equity = float(point.equity)
    if current_key is not None and current_equity is not None:
        month_ends.append((current_key, current_equity))

    returns: list[float] = []
    previous = float(result.initial_equity)
    for _key, ending in month_ends:
        if previous > 0:
            returns.append((ending / previous - 1.0) * 100.0)
        previous = ending
    return returns


def summarize_months(result: BacktestResult, target_pct: float = 5.0) -> MonthlyStats:
    returns = monthly_returns(result)
    if not returns:
        return MonthlyStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    months = len(returns)
    ratio = result.final_equity / result.initial_equity if result.initial_equity > 0 else 0.0
    compounded = ((ratio ** (1.0 / months)) - 1.0) * 100.0 if ratio > 0 else -100.0
    return MonthlyStats(
        months=months,
        compounded_monthly_pct=compounded,
        mean_monthly_pct=mean(returns),
        median_monthly_pct=median(returns),
        positive_month_rate_pct=sum(x > 0 for x in returns) / months * 100.0,
        target_hit_rate_pct=sum(x >= target_pct for x in returns) / months * 100.0,
        worst_month_pct=min(returns),
        best_month_pct=max(returns),
    )


def _oos_slice(bars: list[PriceBar], fraction: float = 0.30, minimum: int = 120) -> list[PriceBar]:
    if not bars:
        return []
    count = max(minimum, int(len(bars) * fraction))
    count = min(len(bars), count)
    return bars[-count:]


def evaluate_monthly_target(
    *,
    symbol: str,
    profile: str,
    timeframe: str,
    duration: str,
    bars: list[PriceBar],
    strategies: list[object],
    scenarios: tuple[ValidationScenario, ...] = DEFAULT_SCENARIOS,
    initial_equity: float = 10_000.0,
    target_pct: float = 5.0,
) -> list[MonthlyTargetRow]:
    rows: list[MonthlyTargetRow] = []
    oos_bars = _oos_slice(bars)
    for strategy in strategies:
        for scenario in scenarios:
            engine = BacktestEngine(
                initial_equity=initial_equity,
                risk_pct=scenario.risk_pct,
                commission_bps=scenario.commission_bps,
                slippage_bps=scenario.slippage_bps,
                max_position_pct=scenario.max_position_pct,
            )
            full = engine.run(bars, strategy)
            full_monthly = summarize_months(full, target_pct=target_pct)
            oos = engine.run(oos_bars, strategy) if len(oos_bars) >= 2 else engine.run([], strategy)
            oos_monthly = summarize_months(oos, target_pct=target_pct)
            pf = full.profit_factor
            finite_pf = 0.0 if pf is None else (999.0 if pf == float("inf") else float(pf))

            # A candidate label is deliberately hard to earn. It is evidence for further
            # Paper validation, never a return promise or permission to increase hard risk.
            candidate = bool(
                full_monthly.months >= 12
                and full_monthly.compounded_monthly_pct >= target_pct
                and full_monthly.median_monthly_pct > 0
                and full_monthly.positive_month_rate_pct >= 60.0
                and full.max_drawdown_pct <= 20.0
                and full.trades >= 20
                and finite_pf >= 1.15
                and oos_monthly.months >= 3
                and oos_monthly.compounded_monthly_pct > 0
                and oos.max_drawdown_pct <= 20.0
            )
            rows.append(MonthlyTargetRow(
                symbol=symbol,
                profile=profile,
                timeframe=timeframe,
                duration=duration,
                strategy=str(strategy.name),
                scenario=scenario.name,
                bars=len(bars),
                months=full_monthly.months,
                total_return_pct=full.total_return_pct,
                compounded_monthly_pct=full_monthly.compounded_monthly_pct,
                mean_monthly_pct=full_monthly.mean_monthly_pct,
                median_monthly_pct=full_monthly.median_monthly_pct,
                positive_month_rate_pct=full_monthly.positive_month_rate_pct,
                target_5pct_hit_rate_pct=full_monthly.target_hit_rate_pct,
                worst_month_pct=full_monthly.worst_month_pct,
                best_month_pct=full_monthly.best_month_pct,
                max_drawdown_pct=full.max_drawdown_pct,
                trades=full.trades,
                profit_factor=full.profit_factor,
                oos_bars=len(oos_bars),
                oos_months=oos_monthly.months,
                oos_return_pct=oos.total_return_pct,
                oos_compounded_monthly_pct=oos_monthly.compounded_monthly_pct,
                oos_target_5pct_hit_rate_pct=oos_monthly.target_hit_rate_pct,
                oos_max_drawdown_pct=oos.max_drawdown_pct,
                candidate_5pct=candidate,
            ))
    return rows


def robustness_rank(row: MonthlyTargetRow) -> float:
    """Rank consistency, not raw return, so extreme overfit results do not dominate."""
    return (
        min(10.0, max(-10.0, row.compounded_monthly_pct)) * 3.0
        + min(100.0, row.positive_month_rate_pct) * 0.18
        + min(100.0, row.target_5pct_hit_rate_pct) * 0.10
        + min(10.0, max(-10.0, row.oos_compounded_monthly_pct)) * 3.5
        - min(50.0, row.max_drawdown_pct) * 0.7
        - min(50.0, row.oos_max_drawdown_pct) * 0.5
        + min(3.0, 0.0 if row.profit_factor is None else row.profit_factor) * 4.0
    )


def write_monthly_target_report(rows: list[MonthlyTargetRow], output_dir: str | Path, stem: str) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / f"{stem}.csv"
    json_path = output / f"{stem}.json"
    fields = list(MonthlyTargetRow.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump([asdict(row) for row in rows], handle, indent=2)
    return csv_path, json_path
