from __future__ import annotations

import argparse
import csv
from pathlib import Path

from backtest.engine import BacktestEngine
from backtest.monthly_target import summarize_months
from research.confluence_gen3 import gen3_windows
from research.confluence_gen4 import Gen4Candidate, admit_candidates, simulate_equal_risk_portfolio
from research.confluence_lab import candidate_strategies
from research.splits import run_window
from run_monthly_target_suite import PROFILES, _cache_path, _load_bars

# Admission must be decided on VALIDATION evidence. Earlier versions admitted on the
# Gen3 TEST columns and then reported the portfolio on that same TEST — leakage.
VALIDATION_COLUMNS = (
    "validation_monthly_pct", "validation_dd_pct", "validation_positive_month_rate_pct", "validation_trades",
)


def load_validation_candidates(source: Path) -> list[Gen4Candidate]:
    """Build admission candidates from VALIDATION metrics only; TEST columns are never read."""
    rows = []
    with source.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in VALIDATION_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"Gen3 report lacks validation columns {missing}; rerun run_confluence_gen3.py "
                "(older reports only allowed admission on TEST metrics)"
            )
        for r in reader:
            rows.append(Gen4Candidate(
                r["symbol"], r["profile"], r["strategy"],
                float(r["validation_monthly_pct"]), float(r["validation_dd_pct"]),
                float(r["validation_positive_month_rate_pct"]), int(r["validation_trades"]),
            ))
    return rows


def _strategy_by_name(name: str):
    for s in candidate_strategies():
        if s.name == name:
            return s
    raise KeyError(name)


def _monthly_returns(result) -> dict[str, float]:
    if not result.equity_curve:
        return {}
    ends = {}
    for p in result.equity_curve:
        ends[str(p.time)[:7]] = p.equity
    keys = sorted(ends)
    if len(keys) < 2:
        return {}
    out = {}
    prev = ends[keys[0]]
    for key in keys[1:]:
        cur = ends[key]
        if prev > 0:
            out[key] = (cur / prev - 1) * 100
        prev = cur
    return out


def run(*, gen3_csv: str, cache_dir: str, output_dir: str, min_trades: int) -> None:
    source = Path(gen3_csv)
    if not source.exists():
        raise FileNotFoundError(f"Gen3 report missing: {source}. Run run_confluence_gen3.py first.")
    rows = load_validation_candidates(source)
    admitted = admit_candidates(rows, min_trades=min_trades)
    print(f"GEN4 PORTFOLIO | gen3_rows={len(rows)} admitted_on_VALIDATION={len(admitted)} "
          f"min_trades={min_trades} shared_capital=True")
    cache = Path(cache_dir)
    series = {}
    audit = []
    for c in admitted:
        p = PROFILES[c.profile]
        path = _cache_path(cache, c.symbol, p)
        if not path.exists():
            print(f"SKIP {c.symbol} {c.profile} cache_missing")
            continue
        bars = _load_bars(path)
        _, _, test = gen3_windows(len(bars))
        strategy = _strategy_by_name(c.strategy)
        engine = BacktestEngine(initial_equity=10_000, risk_pct=.01, max_position_pct=.10)
        result = run_window(engine, bars, strategy, test)
        monthly = _monthly_returns(result)
        if not monthly:
            continue
        series[f"{c.symbol}:{c.profile}:{c.strategy}"] = monthly
        stats = summarize_months(result)
        audit.append((c, stats.compounded_monthly_pct, result.max_drawdown_pct, result.trades))
        print(f"ADMIT {c.symbol:6s} {c.profile:12s} {c.strategy:42s} test/mo={stats.compounded_monthly_pct:6.2f}% "
              f"DD={result.max_drawdown_pct:5.2f}% trades={result.trades}")
    result = simulate_equal_risk_portfolio(series)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "GEN4_ADMITTED.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "profile", "strategy", "test_monthly_pct", "test_dd_pct", "test_trades"])
        for c, m, dd, t in audit:
            w.writerow([c.symbol, c.profile, c.strategy, m, dd, t])
    print("\n=== GEN4 SHARED-CAPITAL PORTFOLIO (TEST, evaluated once) ===")
    print(f"candidates={result.candidates} months={result.months} compounded/mo={result.compounded_monthly_pct:.2f}% "
          f"positive={result.positive_month_rate_pct:.1f}% maxDD={result.max_drawdown_pct:.2f}% "
          f"final_equity={result.final_equity:.2f}")
    print("NOTE: monthly sleeve aggregation; event-level concurrency/correlation is not modelled.")
    print(f"Report: {(out / 'GEN4_ADMITTED.csv').resolve()}")


def main() -> None:
    p = argparse.ArgumentParser(description="Generation 4 shared-capital portfolio: admit on VALIDATION, report TEST once.")
    p.add_argument("--gen3-csv", default="reports/confluence_gen3/CONFLUENCE_GEN3_UNTOUCHED_TEST.csv")
    p.add_argument("--cache-dir", default="reports/history_cache")
    p.add_argument("--output-dir", default="reports/confluence_gen4")
    p.add_argument("--min-trades", type=int, default=12)
    a = p.parse_args()
    run(gen3_csv=a.gen3_csv, cache_dir=a.cache_dir, output_dir=a.output_dir, min_trades=a.min_trades)


if __name__ == "__main__":
    main()
