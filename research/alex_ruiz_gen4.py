from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import sqrt


@dataclass(frozen=True)
class Gen4Candidate:
    symbol: str
    profile: str
    strategy: str
    test_monthly_pct: float
    test_dd_pct: float
    positive_month_rate_pct: float
    trades: int


@dataclass(frozen=True)
class Gen4PortfolioResult:
    candidates: int
    months: int
    compounded_monthly_pct: float
    positive_month_rate_pct: float
    max_drawdown_pct: float
    final_equity: float


def admit_candidates(rows: list[Gen4Candidate], *, min_trades: int = 12, min_positive_month_rate: float = 50.0, max_dd_pct: float = 10.0) -> list[Gen4Candidate]:
    """Evidence gate. Test is used only to decide whether a Gen3 survivor is worth portfolio research."""
    return [
        r for r in rows
        if r.trades >= min_trades
        and r.test_monthly_pct > 0
        and r.positive_month_rate_pct >= min_positive_month_rate
        and r.test_dd_pct <= max_dd_pct
    ]


def _month_key(value: object) -> str:
    if isinstance(value, datetime):
        return f"{value.year:04d}-{value.month:02d}"
    text = str(value)
    return text[:7]


def simulate_equal_risk_portfolio(
    monthly_returns: dict[str, dict[str, float]],
    *,
    initial_equity: float = 10_000.0,
    max_strategy_weight: float = 0.20,
    max_gross_weight: float = 1.0,
) -> Gen4PortfolioResult:
    """Combine realized monthly strategy returns with one shared capital pool.

    The simulator never sums independent account returns. Each active sleeve receives
    equal capital, capped per strategy and by total gross exposure. This is deliberately
    conservative until event-level concurrent-position simulation is introduced.
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    if not 0 < max_strategy_weight <= 1 or not 0 < max_gross_weight <= 1:
        raise ValueError("invalid portfolio weights")
    months = sorted({m for series in monthly_returns.values() for m in series})
    equity = initial_equity
    peak = equity
    max_dd = 0.0
    positive = 0
    used_months = 0
    for month in months:
        active = [series[month] for series in monthly_returns.values() if month in series]
        if not active:
            continue
        weight = min(max_strategy_weight, max_gross_weight / len(active))
        portfolio_return = sum((r / 100.0) * weight for r in active)
        equity *= 1.0 + portfolio_return
        used_months += 1
        if portfolio_return > 0:
            positive += 1
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak else 0.0
        max_dd = max(max_dd, dd)
    compounded = 0.0
    if used_months and equity > 0:
        compounded = ((equity / initial_equity) ** (1.0 / used_months) - 1.0) * 100.0
    return Gen4PortfolioResult(
        candidates=len(monthly_returns),
        months=used_months,
        compounded_monthly_pct=compounded,
        positive_month_rate_pct=(positive / used_months * 100.0) if used_months else 0.0,
        max_drawdown_pct=max_dd * 100.0,
        final_equity=equity,
    )
