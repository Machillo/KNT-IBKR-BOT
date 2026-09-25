"""Run pre-registered pipeline variants on protocol segments (offline, cache only).

Prints one compact row per (variant, profile, segment, cost). Never evaluates HOLDOUT
unless --confirm-holdout is passed together with exactly one --variant.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import mean

from backtest.costs import BASELINE, STRESSED
from research.pipeline_backtest import PipelineBacktest, _key
from research.pipeline_variants import VARIANTS
from research.protocol import DEVELOPMENT_PROFILES, PROTOCOL, SEGMENTS
from run_pipeline_backtest import load

COSTS = {"baseline": BASELINE, "stressed": STRESSED}


def buy_and_hold(data: dict, start, end) -> tuple[float, int]:
    """Equal-weight buy-and-hold of the cohort over [start, end) (reference only)."""
    rets = []
    for bars in data.values():
        window = [b for b in bars if (start is None or _key(b.time) >= start) and (end is None or _key(b.time) < end)]
        if len(window) >= 2 and window[0].open > 0:
            rets.append(window[-1].close / window[0].open - 1)
    return (mean(rets) * 100 if rets else 0.0), len(rets)


def yearly(result) -> str:
    by_year = defaultdict(float)
    for t in result.trade_log:
        by_year[str(t.entry_time)[:4]] += t.pnl
    return " ".join(f"{y}:{v / result.initial_equity * 100:+.1f}%" for y, v in sorted(by_year.items()))


def row(result) -> str:
    pf = "  n/a" if result.profit_factor is None else f"{result.profit_factor:5.2f}"
    sh = "  n/a" if result.sharpe is None else f"{result.sharpe:5.2f}"
    months = result.monthly_returns_pct
    mo = mean(months) if months else 0.0
    return (f"ret={result.total_return_pct:7.2f}% mo={mo:5.2f}% dd={result.max_drawdown_pct:5.1f}% "
            f"sh={sh} pf={pf} n={result.trades:4d} win={result.win_rate_pct:4.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", action="append", choices=list(VARIANTS))
    ap.add_argument("--profile", action="append")
    ap.add_argument("--segment", action="append", choices=list(SEGMENTS))
    ap.add_argument("--cost", action="append", choices=list(COSTS))
    ap.add_argument("--universe", default="all")
    ap.add_argument("--confirm-holdout", action="store_true")
    ap.add_argument("--yearly", action="store_true")
    a = ap.parse_args()
    variants = a.variant or list(VARIANTS)
    profiles = a.profile or list(DEVELOPMENT_PROFILES)
    segments = a.segment or ["train", "validation"]
    costs = a.cost or ["baseline"]
    if "holdout" in segments and (not a.confirm_holdout or len(variants) != 1):
        ap.error("HOLDOUT needs --confirm-holdout and exactly one frozen --variant")
    print(f"EXPERIMENTS | protocol={PROTOCOL} universe={a.universe}")
    for profile in profiles:
        data = load(a.universe, profile, "reports/history_cache")
        for segment in segments:
            start, end = SEGMENTS[segment]
            bh, n = buy_and_hold(data, start, end)
            print(f"[{profile} {segment}] reference equal-weight buy&hold={bh:.1f}% over {n} symbols")
            for cost in costs:
                for name in variants:
                    cfg = VARIANTS[name].variant(cost_model=COSTS[cost])
                    result = PipelineBacktest(data, cfg).run(start, end)
                    line = f"  {name:18s} {cost:8s} {row(result)}"
                    if a.yearly:
                        line += f" | {yearly(result)}"
                    print(line, flush=True)


if __name__ == "__main__":
    main()
