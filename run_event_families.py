"""Round-3 cohort-neutral event studies (offline, cache only). Applies the pre-registered KEEP rule.

Never touches the HOLDOUT (protocol v1). See docs/experiments/LOG.md for the pre-registration.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from research.event_study import run_event_study, summarize
from research.families import FAMILIES, make_signal
from research.protocol import SEGMENTS
from run_pipeline_backtest import load

K_TESTS = 15
VAL_T = 2.71    # one-sided Bonferroni, alpha 0.05 / 15
TRAIN_T = 2.0
MIN_EVENTS = 100
MIN_F1_DATES = 12


def fmt(stats) -> str:
    t = "  n/a" if stats.nw_t is None else f"{stats.nw_t:5.2f}"
    return (f"events={stats.events:5d} dates={stats.dates:4d} net_excess={stats.mean_net_excess_bps:7.1f}bps "
            f"raw={stats.mean_raw_bps:7.1f}bps hit={stats.hit_rate_pct:4.1f}% t={t}")


def study(data, name, segment, horizon=None, **kw):
    signal, default_h = make_signal(name)
    h = horizon or default_h
    events = run_event_study(data, signal, horizon=h, segment=SEGMENTS[segment], **kw)
    return events, summarize(events, h)


def passes(stats, t_min, min_events):
    return stats.nw_t is not None and stats.mean_net_excess_bps > 0 and stats.nw_t >= t_min and stats.events >= min_events


def destruction(daily, name, val_events):
    _, h = FAMILIES[name]
    checks = {}
    checks["cost_x3"] = study(daily, name, "validation", cost_multiplier=3.0)[1]
    checks["entry_delay_2"] = study(daily, name, "validation", entry_delay=2)[1]
    checks["horizon_x0.5"] = study(daily, name, "validation", horizon=max(1, h // 2))[1]
    checks["horizon_x1.5"] = study(daily, name, "validation", horizon=int(h * 1.5))[1]
    ranked = sorted(val_events, key=lambda e: e.net_excess, reverse=True)
    checks["drop_best_5pct"] = summarize(ranked[int(len(ranked) * 0.05):], h)
    by_symbol = defaultdict(float)
    for e in val_events:
        by_symbol[e.symbol] += e.net_excess
    best = max(by_symbol, key=by_symbol.get)
    checks[f"drop_best_symbol_{best}"] = summarize([e for e in val_events if e.symbol != best], h)
    ordered = sorted(val_events, key=lambda e: e.entry_time)
    mid = ordered[len(ordered) // 2].entry_time if ordered else None
    checks["val_first_half"] = summarize([e for e in ordered if e.entry_time < mid], h)
    checks["val_second_half"] = summarize([e for e in ordered if e.entry_time >= mid], h)
    survived = all(s.events > 0 and s.mean_net_excess_bps > 0 for s in checks.values())
    return survived, checks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", action="append", choices=list(FAMILIES))
    a = ap.parse_args()
    daily = load("all", "long_10y", "reports/history_cache")
    swing = load("all", "swing_5y", "reports/history_cache")
    print(f"ROUND 3 | K={K_TESTS} tests on VALIDATION -> VAL t >= {VAL_T}; TRAIN t >= {TRAIN_T}; "
          f"symbols daily={len(daily)} 4h={len(swing)}")
    for name in a.family or list(FAMILIES):
        tr_events, tr = study(daily, name, "train")
        va_events, va = study(daily, name, "validation")
        _, sw = study(swing, name, "validation")
        print(f"\n{name}")
        print(f"  daily TRAIN  {fmt(tr)}")
        print(f"  daily VAL    {fmt(va)}")
        print(f"  4h    VAL    {fmt(sw)}")
        min_events = MIN_EVENTS
        if name.startswith("F1"):
            ok3_train = sorted(tr_events, key=lambda e: e.entry_time)
            mid = ok3_train[len(ok3_train) // 2].entry_time if ok3_train else None
            h = FAMILIES[name][1]
            halves = [summarize([e for e in ok3_train if e.entry_time < mid], h),
                      summarize([e for e in ok3_train if e.entry_time >= mid], h)] if mid else []
            step3 = bool(halves) and all(x.mean_net_excess_bps > 0 for x in halves)
            step2 = passes(va, VAL_T, 0) and va.dates >= MIN_F1_DATES
        else:
            step3 = sw.events > 0 and sw.mean_net_excess_bps > 0
            step2 = passes(va, VAL_T, min_events)
        step1 = passes(tr, TRAIN_T, 1)
        if not (step1 and step2 and step3):
            weak = step1 and va.mean_net_excess_bps > 0 and (va.nw_t or 0) >= VAL_T and va.events < min_events
            verdict = "INCONCLUSIVE (too few events)" if weak else "REJECT"
            print(f"  steps: train={step1} validation={step2} robustness={step3} -> {verdict}")
            continue
        survived, checks = destruction(daily, name, va_events)
        for label, s in checks.items():
            print(f"    destroy {label:28s} {fmt(s)}")
        print(f"  steps 1-3 passed; destruction survived={survived} -> {'KEEP' if survived else 'REJECT'}")


if __name__ == "__main__":
    main()
