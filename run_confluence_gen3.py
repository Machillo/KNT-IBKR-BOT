from __future__ import annotations
import argparse,csv
from dataclasses import asdict
from pathlib import Path
from backtest.universes import VALIDATION_UNIVERSES,universe_symbols
from research.confluence_gen3 import select_and_test
from run_monthly_target_suite import PROFILES,_cache_path,_load_bars

def run(universe:str,profile:str,cache_dir:str,output_dir:str)->None:
    cache=Path(cache_dir); out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
    symbols=universe_symbols(universe); profiles=list(PROFILES.values()) if profile=="all" else [PROFILES[profile]]; results=[]; missing=0
    print("CONFLUENCE GEN3 | chronological train=60% validation=20% untouched_test=20%")
    for symbol in symbols:
        for p in profiles:
            path=_cache_path(cache,symbol,p)
            if not path.exists(): missing+=1; print(f"{symbol:6s} {p.name:12s} SKIPPED cache_missing"); continue
            result=select_and_test(symbol,p.name,_load_bars(path))
            if result is None: print(f"{symbol:6s} {p.name:12s} NO_SURVIVOR"); continue
            results.append(result); print(f"{symbol:6s} {p.name:12s} winner={result.strategy:42s} train={result.train_monthly_pct:7.2f}% val={result.validation_monthly_pct:7.2f}% TEST={result.test_monthly_pct:7.2f}% testDD={result.test_dd_pct:6.2f}% testPos={result.test_positive_month_rate_pct:5.1f}% trades={result.test_trades}")
    ranked=sorted(results,key=lambda r:(r.test_monthly_pct,-r.test_dd_pct),reverse=True); csv_path=out/"CONFLUENCE_GEN3_UNTOUCHED_TEST.csv"
    if ranked:
        with csv_path.open("w",newline="",encoding="utf-8") as fh:
            w=csv.DictWriter(fh,fieldnames=list(asdict(ranked[0]))); w.writeheader(); [w.writerow(asdict(r)) for r in ranked]
    print("\n=== GEN3 UNTOUCHED TEST TOP 20 ===")
    for i,r in enumerate(ranked[:20],1): print(f"{i:2d}. {r.symbol:6s} {r.profile:12s} {r.strategy:42s} train={r.train_monthly_pct:7.2f}% val={r.validation_monthly_pct:7.2f}% TEST={r.test_monthly_pct:7.2f}% DD={r.test_dd_pct:6.2f}% positive={r.test_positive_month_rate_pct:5.1f}% trades={r.test_trades}")
    print(f"\nSUMMARY | datasets={len(symbols)*len(profiles)} survivors={len(ranked)} positive_test={sum(r.test_monthly_pct>0 for r in ranked)} cache_missing={missing}"); print(f"Report: {csv_path.resolve()}")

def main()->None:
    p=argparse.ArgumentParser(description="Leakage-safe confluence Generation 3 selection and untouched final test."); p.add_argument("--universe",choices=[*VALIDATION_UNIVERSES.keys(),"all"],default="all"); p.add_argument("--profile",choices=[*PROFILES.keys(),"all"],default="all"); p.add_argument("--cache-dir",default="reports/history_cache"); p.add_argument("--output-dir",default="reports/confluence_gen3"); a=p.parse_args(); run(a.universe,a.profile,a.cache_dir,a.output_dir)
if __name__=="__main__": main()
