"""Offline pipeline backtest on reports/history_cache (no IBKR connection).

Default segments follow the calendar protocol in research/protocol.py (TRAIN <
2023-09-01 <= VALIDATION < 2025-03-01 <= HOLDOUT), shared by every cached profile.
``--split fraction`` reproduces legacy per-profile 60/20/20 splits for diagnostics.
HOLDOUT is only evaluated with --confirm-holdout, for a configuration frozen in
docs/experiments BEFORE running it.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

from backtest.costs import BASELINE, SEVERE, STRESSED
from backtest.universes import VALIDATION_UNIVERSES, universe_symbols
from research.pipeline_backtest import PipelineBacktest, PipelineConfig, time_split
from research.pipeline_variants import VARIANTS
from research.protocol import PROTOCOL, SEGMENTS
from run_monthly_target_suite import PROFILES, _cache_path, _load_bars

COSTS = {"baseline": BASELINE, "stressed": STRESSED, "severe": SEVERE}


def load(universe: str, profile: str, cache_dir: str) -> dict:
    p = PROFILES[profile]
    data = {}
    for symbol in universe_symbols(universe):
        path = _cache_path(Path(cache_dir), symbol, p)
        if path.exists():
            data[symbol] = _load_bars(path)
    return data


def segment_bounds(bt: PipelineBacktest, segment: str, split: str = "calendar"):
    """Calendar protocol boundaries (default) or legacy per-profile fractions."""
    if split == "calendar":
        return SEGMENTS[segment]
    v, h = time_split(bt.timeline())
    return {"train": (None, v), "validation": (v, h), "development": (None, h), "holdout": (h, None)}[segment]


def summarize(result, *, detail: bool = True) -> str:
    pf = "n/a" if result.profit_factor is None else f"{result.profit_factor:.2f}"
    sh = "n/a" if result.sharpe is None else f"{result.sharpe:.2f}"
    so = "n/a" if result.sortino is None else f"{result.sortino:.2f}"
    cg = "n/a" if result.cagr_pct is None else f"{result.cagr_pct:.2f}%"
    months = result.monthly_returns_pct
    lines = [
        f"{result.config}: ret={result.total_return_pct:.2f}% cagr={cg} maxDD={result.max_drawdown_pct:.2f}% "
        f"sharpe={sh} sortino={so} trades={result.trades} win={result.win_rate_pct:.1f}% pf={pf} "
        f"exp/trade={result.expectancy:.2f} exposure={result.exposure_pct:.1f}% costs={result.total_costs:.0f} "
        f"decisions={result.decisions} no_trade={result.no_trade_decisions} port_rejects={result.rejected_by_portfolio}",
    ]
    if months:
        lines.append(f"  months={len(months)} mean/mo={mean(months):.2f}% median/mo={median(months):.2f}% "
                     f"positive={sum(m > 0 for m in months) / len(months) * 100:.0f}% worst={min(months):.2f}% best={max(months):.2f}%")
    if detail and result.trade_log:
        by = defaultdict(list)
        for t in result.trade_log:
            by[t.strategy].append(t.pnl)
        parts = [f"{k}:{len(v)}/{sum(v):+.0f}" for k, v in sorted(by.items(), key=lambda kv: sum(kv[1]))]
        lines.append("  by_strategy(n/pnl): " + " ".join(parts))
        by_r = defaultdict(list)
        for t in result.trade_log:
            by_r[t.regime].append(t.pnl)
        lines.append("  by_regime(n/pnl): " + " ".join(f"{k}:{len(v)}/{sum(v):+.0f}" for k, v in sorted(by_r.items())))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=list(PROFILES), default="long_10y")
    ap.add_argument("--universe", choices=[*VALIDATION_UNIVERSES, "all"], default="all")
    ap.add_argument("--segment", choices=["train", "validation", "development", "holdout"], default="train")
    ap.add_argument("--variant", choices=list(VARIANTS), default="live_default")
    ap.add_argument("--cost", choices=list(COSTS), default="baseline")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--cache-dir", default="reports/history_cache")
    ap.add_argument("--confirm-holdout", action="store_true")
    ap.add_argument("--split", choices=["calendar", "fraction"], default="calendar")
    a = ap.parse_args()
    if a.segment == "holdout" and not a.confirm_holdout:
        ap.error("HOLDOUT is single-use. Freeze the configuration in docs/experiments first, then pass --confirm-holdout.")
    data = load(a.universe, a.profile, a.cache_dir)
    config: PipelineConfig = VARIANTS[a.variant].variant(cost_model=COSTS[a.cost], initial_equity=a.equity)
    bt = PipelineBacktest(data, config)
    start, end = segment_bounds(bt, a.segment, a.split)
    print(f"PIPELINE | protocol={PROTOCOL if a.split == 'calendar' else 'fraction'} profile={a.profile} universe={a.universe} symbols={len(data)} segment={a.segment.upper()} "
          f"cost={a.cost} equity={a.equity:.0f} window=[{start or 'begin'} .. {end or 'end'})")
    if a.segment == "holdout":
        print("*** HOLDOUT EVALUATION — record the result; do not tune against it. ***")
    print(summarize(bt.run(start, end)))


if __name__ == "__main__":
    main()
