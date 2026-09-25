"""Performance metrics with a documented, defensible methodology.

Sharpe and Sortino are computed from DAILY mark-to-market equity returns
(last equity point of each calendar day; the first day is measured against the
initial equity), annualized with sqrt(252), risk-free rate 0. Days without
bars (weekends/holidays) are not filled, so the ratio reflects trading days.

The previous engine reported mean/std of per-trade returns times sqrt(N); that
number grows with the trade count and is not a Sharpe ratio. It is still
available as ``trade_sharpe`` for comparison only.
"""
from __future__ import annotations

from datetime import date, datetime
from math import sqrt

TRADING_DAYS = 252
# Standard deviations below this are treated as zero (float noise on constant series).
_MIN_STD = 1e-12


def as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def daily_equity(points, initial_equity: float) -> list[tuple[date, float]]:
    """Last equity per calendar day from (time, equity) points."""
    by_day: dict[date, float] = {}
    order: list[date] = []
    for time, equity in points:
        dt = as_datetime(time)
        if dt is None:
            return []
        day = dt.date()
        if day not in by_day:
            order.append(day)
        by_day[day] = float(equity)
    return [(day, by_day[day]) for day in order]


def period_returns(values: list[float], initial: float) -> list[float]:
    returns: list[float] = []
    previous = float(initial)
    for value in values:
        if previous > 0:
            returns.append(value / previous - 1.0)
        previous = value
    return returns


def sharpe(returns: list[float], periods_per_year: int = TRADING_DAYS) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if sqrt(max(var, 0.0)) < _MIN_STD:
        return None
    return mean / sqrt(var) * sqrt(periods_per_year)


def sortino(returns: list[float], periods_per_year: int = TRADING_DAYS) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    downside = sum(min(r, 0.0) ** 2 for r in returns) / len(returns)
    if sqrt(downside) < _MIN_STD:
        return None
    return mean / sqrt(downside) * sqrt(periods_per_year)


def trade_sharpe(trade_returns: list[float]) -> float | None:
    """Legacy per-trade statistic (NOT a Sharpe ratio); kept for comparison."""
    if len(trade_returns) < 2:
        return None
    m = sum(trade_returns) / len(trade_returns)
    var = sum((x - m) ** 2 for x in trade_returns) / (len(trade_returns) - 1)
    return None if sqrt(max(var, 0.0)) < _MIN_STD else m / sqrt(var) * sqrt(len(trade_returns))


def cagr(initial: float, final: float, first: date | None, last: date | None) -> float | None:
    if initial <= 0 or final <= 0 or first is None or last is None:
        return None
    days = (last - first).days
    if days < 30:
        return None
    return (final / initial) ** (365.25 / days) - 1.0
