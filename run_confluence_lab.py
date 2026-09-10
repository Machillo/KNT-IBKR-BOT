from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path
from backtest.monthly_target import evaluate_monthly_target,robustness_rank,write_monthly_target_report
from backtest.universes import VALIDATION_UNIVERSES,universe_symbols
from backtest.validation import DEFAULT_SCENARIOS
from research.confluence_lab import candidate_strategies
from run_monthly_target_suite import PROFILES,_cache_path,_load_bars

def _baseline_only(): return tuple(s for s in DEFAULT_SCENARIOS if s.name=="baseline")

def run(*,universe:str,profile:str,target_pct:float,cache_dir:str,output_dir:str,topn:int)->None:
    cache=Path(cache_dir); out=Path(output_dir); out.mkdir(parents=True,exist_ok=True); symbols=universe_symbols(universe); profiles=list(PROFILES.values()) if profile=="all" else [PROFILES[profile]]; strategies=candidate_strategies(); scenarios=_baseline_only()
    if not scenarios: raise RuntimeError("baseline validation scenario not found")
    all_rows=[]; datasets=missing=local_backtests=0
    print(f"CONFLUENCE LAB GEN2 | variants={len(strategies)} datasets={len(symbols)*len(profiles)} baseline_screen=True")
    for symbol in symbols:
        for p in profiles:
            cached=_cache_path(cache,symbol,p)
            if not cached.exists(): print(f"{symbol:6s} {p.name:12s} SKIPPED cache_missing"); missing+=1; continue
            bars=_load_bars(cached)
            if len(bars)<180: print(f"{symbol:6s} {p.name:12s} SKIPPED insufficient_history bars={len(bars)}"); continue
            rows=evaluate_monthly_target(symbol=symbol,profile=p.name,timeframe=p.bar_size,duration=p.duration,bars=bars,strategies=strategies,scenarios=scenarios,target_pct=target_pct); all_rows.extend(rows); datasets+=1; local_backtests+=len(rows)*2; best=max(rows,key=robustness_rank)
            print(f"{symbol:6s} {p.name:12s} best={best.strategy:42s} cmpd/mo={best.compounded_monthly_pct:7.2f}% median={best.median_monthly_pct:7.2f}% positive={best.positive_month_rate_pct:5.1f}% DD={best.max_drawdown_pct:6.2f}% OOS/mo={best.oos_compounded_monthly_pct:7.2f}% OOS_DD={best.oos_max_drawdown_pct:6.2f}%")
    if not all_rows: print("No rows produced. Run the monthly target suite first to populate reports/history_cache."); return
    ranked=sorted(all_rows,key=robustness_rank,reverse=True); write_monthly_target_report(ranked,out,"CONFLUENCE_GEN2_ALL"); write_monthly_target_report(ranked[:topn],out,"CONFLUENCE_GEN2_TOP")
    by_strategy=defaultdict(list)
    for row in all_rows: by_strategy[row.strategy].append(row)
    summary=[]
    for strategy,rows in by_strategy.items():
        profitable=sum(r.oos_compounded_monthly_pct>0 for r in rows); avg_oos=sum(r.oos_compounded_monthly_pct for r in rows)/len(rows); avg_full=sum(r.compounded_monthly_pct for r in rows)/len(rows); avg_dd=sum(r.max_drawdown_pct for r in rows)/len(rows); score=avg_oos*4+avg_full*2+profitable/len(rows)*10-avg_dd*.5; summary.append((score,strategy,len(rows),avg_full,avg_oos,profitable/len(rows)*100,avg_dd))
    summary.sort(reverse=True)
    print("\n=== GEN2 TOP 20 ROWS ===")
    for i,row in enumerate(ranked[:20],1): print(f"{i:2d}. {row.symbol:6s} {row.profile:12s} {row.strategy:42s} cmpd/mo={row.compounded_monthly_pct:7.2f}% median={row.median_monthly_pct:7.2f}% positive={row.positive_month_rate_pct:5.1f}% DD={row.max_drawdown_pct:6.2f}% OOS/mo={row.oos_compounded_monthly_pct:7.2f}% OOS_DD={row.oos_max_drawdown_pct:6.2f}%")
    print("\n=== GEN2 CROSS-DATASET TOP 15 VARIANTS ===")
    for i,(_,strategy,n,avg_full,avg_oos,oos_positive,avg_dd) in enumerate(summary[:15],1): print(f"{i:2d}. {strategy:42s} datasets={n:3d} avg_full={avg_full:6.2f}% avg_OOS={avg_oos:6.2f}% OOS_positive={oos_positive:5.1f}% avg_DD={avg_dd:5.2f}%")
    print(f"\nSUMMARY | variants={len(strategies)} datasets={datasets} rows={len(all_rows)} local_backtests~{local_backtests} cache_missing={missing} target={target_pct:.2f}%/month"); print(f"Reports: {out.resolve()}")

def main()->None:
    p=argparse.ArgumentParser(description="Generation 2 bounded confluence parameter sweep using cached history only."); p.add_argument("--universe",choices=[*VALIDATION_UNIVERSES.keys(),"all"],default="all"); p.add_argument("--profile",choices=[*PROFILES.keys(),"all"],default="all"); p.add_argument("--target-monthly-pct",type=float,default=5.0); p.add_argument("--cache-dir",default="reports/history_cache"); p.add_argument("--output-dir",default="reports/confluence_gen2"); p.add_argument("--topn",type=int,default=200); a=p.parse_args(); run(universe=a.universe,profile=a.profile,target_pct=a.target_monthly_pct,cache_dir=a.cache_dir,output_dir=a.output_dir,topn=a.topn)
if __name__=="__main__": main()
