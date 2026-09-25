"""Compare the live selector with random-entry null models (offline, cache only)."""
from __future__ import annotations

import argparse
from statistics import mean, pstdev

from research.null_models import RandomLongStrategy
from research.pipeline_backtest import PipelineBacktest
from research.pipeline_variants import VARIANTS
from research.protocol import DEVELOPMENT_PROFILES, SEGMENTS
from run_experiments import row
from run_pipeline_backtest import load


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--p", type=float, default=0.1)
    ap.add_argument("--segment", choices=["train", "validation", "development"], default="validation")
    a = ap.parse_args()
    start, end = SEGMENTS[a.segment]
    for profile in DEVELOPMENT_PROFILES:
        data = load("all", profile, "reports/history_cache")
        live = PipelineBacktest(data, VARIANTS["live_default"]).run(start, end)
        print(f"[{profile} {a.segment}] live_default  {row(live)}", flush=True)
        rets, pfs = [], []
        for seed in range(a.seeds):
            bt = PipelineBacktest(data, VARIANTS["live_default"].variant(name=f"null_s{seed}"))
            bt.selector.strategies = [RandomLongStrategy(seed, a.p)]
            r = bt.run(start, end)
            rets.append(r.total_return_pct)
            pfs.append(r.profit_factor or 0.0)
            print(f"  null seed={seed:<3d}  {row(r)}", flush=True)
        better = sum(x >= live.total_return_pct for x in rets)
        print(f"  NULL mean ret={mean(rets):.2f}% sd={pstdev(rets):.2f}% mean pf={mean(pfs):.3f} | "
              f"null runs >= live: {better}/{len(rets)}")


if __name__ == "__main__":
    main()
