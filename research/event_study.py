"""Cohort-neutral event studies for signal families (research only).

For every event (symbol, bar i) produced by a signal function that sees ONLY bars[:i+1]:

* entry at the OPEN of bar ``i + entry_delay`` (default 1 = next bar),
* exit at the CLOSE of bar ``i + entry_delay + horizon - 1``,
* raw return R = exit / entry - 1 (sign-adjusted for shorts),
* benchmark B = equal-weight mean of the same-window return of every OTHER cohort symbol with
  bars on both dates (removes the cohort's common drift: beta and most of the survivorship
  tailwind that inflated long-only backtests),
* net excess = R - B - round-trip cost.

Statistics aggregate events per entry date (cross-sectional averaging) and use a Newey-West
t-statistic over the date series with lag = horizon - 1 (overlapping windows).
Windows are assigned to protocol segments by BOTH entry and exit dates, so no VALIDATION event
can extend into the HOLDOUT.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from math import sqrt
from typing import Callable

from backtest.costs import BASELINE, CostModel
from backtest.metrics import as_datetime
from market.history import PriceBar

# signal(symbol, bars_so_far, context) -> +1 long, -1 short, 0 nothing
SignalFn = Callable[[str, list[PriceBar], "StudyContext"], int]


def _day(bar: PriceBar) -> date:
    dt = as_datetime(bar.time)
    if dt is None:
        raise ValueError(f"unparseable bar time {bar.time!r}")
    return dt.date() if isinstance(dt, datetime) else dt


def _stamp(bar: PriceBar) -> datetime:
    dt = as_datetime(bar.time)
    return dt.replace(tzinfo=None)


@dataclass(frozen=True)
class EventResult:
    symbol: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    side: int
    raw_return: float
    benchmark: float
    net_excess: float


@dataclass(frozen=True)
class StudyStats:
    events: int
    dates: int
    mean_net_excess_bps: float
    mean_raw_bps: float
    hit_rate_pct: float
    nw_t: float | None


class StudyContext:
    """Cross-sectional helpers available to signals — computed ONLY from bars up to each time."""

    def __init__(self, data: dict[str, list[PriceBar]]) -> None:
        self.data = data
        self.index = {s: {_stamp(b): k for k, b in enumerate(bars)} for s, bars in data.items()}

    def bars_through(self, symbol: str, t: datetime) -> list[PriceBar] | None:
        k = self.index.get(symbol, {}).get(t)
        return None if k is None else self.data[symbol][:k + 1]

    def cross_section(self, t: datetime, name: str, fn: Callable[[list[PriceBar], int], float | None]) -> dict[str, float]:
        """{symbol: fn(bars, k)} for every symbol with a bar stamped t, where k is that bar's
        index — ``fn`` must only read bars[0..k]. Cached per (t, name)."""
        cache = self.__dict__.setdefault("_xs_cache", {})
        key = (t, name)
        if key not in cache:
            values = {}
            for symbol, idx in self.index.items():
                k = idx.get(t)
                if k is None:
                    continue
                value = fn(self.data[symbol], k)
                if value is not None:
                    values[symbol] = value
            cache[key] = values
        return cache[key]


def round_trip_cost(costs: CostModel) -> float:
    """Fractional cost of a round trip: marketable in and out plus the sell fee (commission is
    size-dependent; ~1-2 bps for typical sizes at the IBKR fixed schedule is added separately)."""
    has_commission = costs.commission_per_share > 0 or costs.commission_min > 0 or costs.commission_bps > 0
    commission_allowance = 0.0002 if has_commission else 0.0
    return 2 * costs.marketable_bps / 10_000 + costs.sell_fee_bps / 10_000 + commission_allowance


def run_event_study(data: dict[str, list[PriceBar]], signal: SignalFn, *, horizon: int,
                    entry_delay: int = 1, costs: CostModel = BASELINE, cost_multiplier: float = 1.0,
                    segment: tuple[datetime | None, datetime | None] = (None, None),
                    sample_every: int = 1) -> list[EventResult]:
    if horizon < 1 or entry_delay < 1:
        raise ValueError("horizon and entry_delay must be >= 1")
    ctx = StudyContext(data)
    start, end = segment
    cost = round_trip_cost(costs) * cost_multiplier
    # Window returns of every symbol keyed by (entry_stamp, exit_stamp) for the benchmark.
    window_returns: dict[tuple[datetime, datetime], dict[str, float]] = defaultdict(dict)
    stamps = {s: [_stamp(b) for b in bars] for s, bars in data.items()}

    def window(symbol: str, i: int) -> tuple[datetime, datetime, float] | None:
        bars = data[symbol]
        a, b = i + entry_delay, i + entry_delay + horizon - 1
        if b >= len(bars) or bars[a].open <= 0:
            return None
        return stamps[symbol][a], stamps[symbol][b], bars[b].close / bars[a].open - 1

    for symbol, bars in data.items():
        for i in range(len(bars)):
            w = window(symbol, i)
            if w is not None:
                window_returns[(w[0], w[1])][symbol] = w[2]

    events: list[EventResult] = []
    for symbol, bars in data.items():
        for i in range(0, len(bars), max(1, sample_every)):
            w = window(symbol, i)
            if w is None:
                continue
            entry_t, exit_t, raw = w
            if start is not None and entry_t < start:
                continue
            if end is not None and exit_t >= end:
                continue
            side = signal(symbol, bars[:i + 1], ctx)
            if side == 0:
                continue
            others = [r for s, r in window_returns[(entry_t, exit_t)].items() if s != symbol]
            if not others:
                continue
            bench = sum(others) / len(others)
            excess = side * (raw - bench)
            events.append(EventResult(symbol, stamps[symbol][i], entry_t, exit_t, side, side * raw, side * bench,
                                      excess - cost))
    return events


def newey_west_t(series: list[float], lag: int) -> float | None:
    n = len(series)
    if n < 3:
        return None
    m = sum(series) / n
    d = [x - m for x in series]
    gamma0 = sum(x * x for x in d) / n
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        cov = sum(d[t] * d[t - k] for t in range(k, n)) / n
        var += 2 * (1 - k / (lag + 1)) * cov
    if var <= 1e-24:
        return None
    return m / sqrt(var / n)


def summarize(events: list[EventResult], horizon: int) -> StudyStats:
    if not events:
        return StudyStats(0, 0, 0.0, 0.0, 0.0, None)
    by_date: dict[datetime, list[float]] = defaultdict(list)
    for e in events:
        by_date[e.entry_time].append(e.net_excess)
    series = [sum(v) / len(v) for _, v in sorted(by_date.items())]
    return StudyStats(
        events=len(events),
        dates=len(series),
        mean_net_excess_bps=sum(e.net_excess for e in events) / len(events) * 10_000,
        mean_raw_bps=sum(e.raw_return for e in events) / len(events) * 10_000,
        hit_rate_pct=sum(e.net_excess > 0 for e in events) / len(events) * 100,
        nw_t=newey_west_t(series, max(0, horizon - 1)),
    )
