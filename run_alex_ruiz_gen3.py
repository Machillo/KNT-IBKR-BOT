from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from pathlib import Path

from backtest.universes import VALIDATION_UNIVERSES, universe_symbols
from research.alex_ruiz_gen3 import select_and_test
from run_monthly_target_suite import PROFILES, _cache_path, _load_bars


def run(universe: str, profile: str, cache_dir: str, output_dir: str) -> None:
    cache = Path(cache_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    symbols = universe_symbols(universe)
    profiles = list(PROFILES.values()) if profile == "all" else [PROFILES[profile]]
    results = []
    missing = 0

    print("ALEX RUIZ LAB GEN3 | chronological train=60% validation=20% untouched_test=20%")
    for symbol in symbols:
        for p in profiles:
            path = _cache_path(cache, symbol, p)
            if not path.exists():
                missing += 1
                print(f"{symbol:6s} {p.name:12s} SKIPPED cache_missing")
                continue
            bars = _load_bars(path)
            result = select_and_test(symbol, p.name, bars)
            if result is None:
                print(f"{symbol:6s} {p.name:12s} NO_SURVIVOR")
                continue
            results.append(result)
            print(
                f"{symbol:6s} {p.name:12s} winner={result.strategy:42s} "
                f"train={result.train_monthly_pct:7.2f}% val={result.validation_monthly_pct:7.2f}% "
                f"TEST={result.test_monthly_pct:7.2f}% testDD={result.test_dd_pct:6.2f}% "
                f"testPos={result.test_positive_month_rate_pct:5.1f}% trades={result.test_trades}"
            )

    ranked = sorted(results, key=lambda r: (r.test_monthly_pct, -r.test_dd_pct), reverse=True)
    csv_path = out / "ALEX_GEN3_UNTOUCHED_TEST.csv"
    if ranked:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(ranked[0])))
            writer.writeheader()
            for row in ranked:
                writer.writerow(asdict(row))

    print("\n=== GEN3 UNTOUCHED TEST TOP 20 ===")
    for i, row in enumerate(ranked[:20], 1):
        print(
            f"{i:2d}. {row.symbol:6s} {row.profile:12s} {row.strategy:42s} "
            f"train={row.train_monthly_pct:7.2f}% val={row.validation_monthly_pct:7.2f}% "
            f"TEST={row.test_monthly_pct:7.2f}% DD={row.test_dd_pct:6.2f}% "
            f"positive={row.test_positive_month_rate_pct:5.1f}% trades={row.test_trades}"
        )
    positive = sum(r.test_monthly_pct > 0 for r in ranked)
    print(f"\nSUMMARY | datasets={len(symbols)*len(profiles)} survivors={len(ranked)} positive_test={positive} cache_missing={missing}")
    print(f"Report: {csv_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Leakage-safe Alex Ruiz-inspired Generation 3 selection and untouched final test.")
    parser.add_argument("--universe", choices=[*VALIDATION_UNIVERSES.keys(), "all"], default="all")
    parser.add_argument("--profile", choices=[*PROFILES.keys(), "all"], default="all")
    parser.add_argument("--cache-dir", default="reports/history_cache")
    parser.add_argument("--output-dir", default="reports/alex_ruiz_gen3")
    args = parser.parse_args()
    run(args.universe, args.profile, args.cache_dir, args.output_dir)


if __name__ == "__main__":
    main()
