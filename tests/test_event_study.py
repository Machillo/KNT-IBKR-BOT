from datetime import datetime, timedelta
from random import Random

import pytest

from backtest.costs import CostModel
from market.history import PriceBar
from research.event_study import newey_west_t, run_event_study, summarize

D0 = datetime(2020, 1, 1)
ZERO = CostModel.zero()


def cohort(n_symbols=8, n=400, seed=1, plant=None):
    rng = Random(seed)
    data = {}
    for s in range(n_symbols):
        price, bars = 100.0, []
        for i in range(n):
            o = price
            drift = 0.01 if plant and s == 0 and (i % 20) in (1, 2, 3) else 0.0
            c = o * (1 + drift + rng.gauss(0.0005, 0.01))
            bars.append(PriceBar(D0 + timedelta(days=i), o, max(o, c), min(o, c), c, 1e6))
            price = c
        data[f"S{s}"] = bars
    return data


def planted_signal(symbol, bars, ctx):
    return 1 if symbol == "S0" and (len(bars) - 1) % 20 == 0 else 0


def test_detects_a_planted_edge_and_not_a_null_one():
    planted = summarize(run_event_study(cohort(plant=True, n=1000), planted_signal, horizon=3, costs=ZERO), 3)
    null = summarize(run_event_study(cohort(plant=False, n=1000), planted_signal, horizon=3, costs=ZERO), 3)
    assert planted.mean_net_excess_bps > 200 and planted.nw_t > 3
    assert abs(null.nw_t or 0) < 3


def test_signal_sees_only_past_bars_and_entry_is_next_open():
    seen = []

    def spy(symbol, bars, ctx):
        seen.append((symbol, len(bars), bars[-1].time))
        return 1 if symbol == "S1" and len(bars) == 50 else 0

    data = cohort()
    events = run_event_study(data, spy, horizon=5, costs=ZERO)
    event = events[0]
    assert event.signal_time == data["S1"][49].time and event.entry_time == data["S1"][50].time
    assert event.raw_return == pytest.approx(data["S1"][54].close / data["S1"][50].open - 1)
    assert all(length >= 1 for _, length, _ in seen)


def test_benchmark_removes_common_drift():
    # Every symbol rises identically: raw returns are positive, excess must be ~0.
    data = {f"S{s}": [PriceBar(D0 + timedelta(days=i), 100 * 1.01 ** i, 100 * 1.01 ** i, 100 * 1.01 ** i,
                               100 * 1.01 ** i, 1) for i in range(100)] for s in range(5)}
    events = run_event_study(data, lambda s, b, c: 1, horizon=5, costs=ZERO)
    stats = summarize(events, 5)
    assert stats.mean_raw_bps > 300 and abs(stats.mean_net_excess_bps) < 1e-6


def test_segments_exclude_windows_that_cross_the_boundary():
    data = cohort()
    end = data["S0"][200].time
    events = run_event_study(data, lambda s, b, c: 1, horizon=10, costs=ZERO, segment=(None, end))
    assert events and max(e.exit_time for e in events) < end


def test_costs_reduce_net_excess_and_cross_section_is_point_in_time():
    data = cohort()
    free = summarize(run_event_study(data, lambda s, b, c: 1, horizon=5, costs=ZERO), 5)
    paid = summarize(run_event_study(data, lambda s, b, c: 1, horizon=5), 5)
    assert paid.mean_net_excess_bps < free.mean_net_excess_bps

    def xs_signal(symbol, bars, ctx):
        values = ctx.cross_section(bars[-1].time, "k", lambda b, k: float(k))
        assert all(v == len(bars) - 1 for v in values.values())  # never beyond the current bar
        return 0

    run_event_study(data, xs_signal, horizon=5, costs=ZERO)


def test_newey_west_matches_plain_t_without_autocorrelation():
    series = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01, 0.005, 0.01]
    n, m = len(series), sum(series) / len(series)
    plain = m / ((sum((x - m) ** 2 for x in series) / n) / n) ** 0.5
    assert newey_west_t(series, 0) == pytest.approx(plain)


def test_t_stat_needs_enough_dates_and_lag_follows_real_overlap():
    from research.event_study import MIN_DATES, overlap_lag

    few = summarize(run_event_study(cohort(plant=True), planted_signal, horizon=3, costs=ZERO), 3)
    assert few.dates < MIN_DATES and few.nw_t is None
    daily_overlap = run_event_study(cohort(), lambda s, b, c: 1 if s == "S1" else 0, horizon=5, costs=ZERO)
    assert overlap_lag(daily_overlap) == 4          # consecutive daily windows overlap 4 later dates
    monthly = run_event_study(cohort(n=1000), lambda s, b, c: 1 if s == "S1" and len(b) % 25 == 0 else 0,
                              horizon=21, costs=ZERO)
    assert overlap_lag(monthly) == 0                 # non-overlapping rebalances: no NW inflation


def test_holdout_bars_are_not_visible_to_signals():
    data = cohort()
    end = data["S0"][200].time
    seen = []
    run_event_study(data, lambda s, b, c: seen.append(b[-1].time) or 0, horizon=5, costs=ZERO, segment=(None, end))
    assert max(seen) < end
