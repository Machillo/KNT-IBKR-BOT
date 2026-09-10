from __future__ import annotations
import argparse,csv
from pathlib import Path
from backtest.engine import BacktestEngine
from backtest.monthly_target import summarize_months
from research.confluence_gen3 import split_train_validation_test
from research.confluence_gen4 import Gen4Candidate,admit_candidates,simulate_equal_risk_portfolio
from research.confluence_lab import candidate_strategies
from run_monthly_target_suite import PROFILES,_cache_path,_load_bars

def _strategy_by_name(name:str):
    for s in candidate_strategies():
        if s.name==name:return s
    raise KeyError(name)

def _monthly_returns(result)->dict[str,float]:
    if not result.equity_curve:return {}
    ends={}
    for p in result.equity_curve: ends[str(p.time)[:7]]=p.equity
    keys=sorted(ends)
    if len(keys)<2:return {}
    out={}; prev=ends[keys[0]]
    for key in keys[1:]:
        cur=ends[key]
        if prev>0: out[key]=(cur/prev-1)*100
        prev=cur
    return out

def run(*,gen3_csv:str,cache_dir:str,output_dir:str,min_trades:int)->None:
    source=Path(gen3_csv)
    if not source.exists(): raise FileNotFoundError(f"Gen3 report missing: {source}. Run run_confluence_gen3.py first.")
    rows=[]
    with source.open(newline="",encoding="utf-8") as fh:
        for r in csv.DictReader(fh): rows.append(Gen4Candidate(r["symbol"],r["profile"],r["strategy"],float(r["test_monthly_pct"]),float(r["test_dd_pct"]),float(r["test_positive_month_rate_pct"]),int(r["test_trades"])))
    admitted=admit_candidates(rows,min_trades=min_trades); print(f"GEN4 PORTFOLIO | gen3_rows={len(rows)} admitted={len(admitted)} min_trades={min_trades} shared_capital=True")
    cache=Path(cache_dir); series={}; audit=[]
    for c in admitted:
        p=PROFILES[c.profile]; path=_cache_path(cache,c.symbol,p)
        if not path.exists(): print(f"SKIP {c.symbol} {c.profile} cache_missing"); continue
        _,_,test=split_train_validation_test(_load_bars(path)); strategy=_strategy_by_name(c.strategy)
        result=BacktestEngine(initial_equity=10_000,risk_pct=.01,commission_bps=2,slippage_bps=2,max_position_pct=.10).run(test,strategy); monthly=_monthly_returns(result)
        if not monthly: continue
        series[f"{c.symbol}:{c.profile}:{c.strategy}"]=monthly; stats=summarize_months(result); audit.append((c,stats.compounded_monthly_pct,result.max_drawdown_pct,result.trades)); print(f"ADMIT {c.symbol:6s} {c.profile:12s} {c.strategy:42s} test/mo={stats.compounded_monthly_pct:6.2f}% DD={result.max_drawdown_pct:5.2f}% trades={result.trades}")
    result=simulate_equal_risk_portfolio(series); out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
    with (out/"GEN4_ADMITTED.csv").open("w",newline="",encoding="utf-8") as fh:
        w=csv.writer(fh); w.writerow(["symbol","profile","strategy","test_monthly_pct","test_dd_pct","test_trades"]); [w.writerow([c.symbol,c.profile,c.strategy,m,dd,t]) for c,m,dd,t in audit]
    print("\n=== GEN4 SHARED-CAPITAL PORTFOLIO ==="); print(f"candidates={result.candidates} months={result.months} compounded/mo={result.compounded_monthly_pct:.2f}% positive={result.positive_month_rate_pct:.1f}% maxDD={result.max_drawdown_pct:.2f}% final_equity={result.final_equity:.2f}"); print("NOTE: conservative monthly sleeve aggregation; event-level concurrency/correlation is the next validation layer."); print(f"Report: {(out/'GEN4_ADMITTED.csv').resolve()}")

def main()->None:
    p=argparse.ArgumentParser(description="Generation 4 contextual shared-capital portfolio research."); p.add_argument("--gen3-csv",default="reports/confluence_gen3/CONFLUENCE_GEN3_UNTOUCHED_TEST.csv"); p.add_argument("--cache-dir",default="reports/history_cache"); p.add_argument("--output-dir",default="reports/confluence_gen4"); p.add_argument("--min-trades",type=int,default=12); a=p.parse_args(); run(gen3_csv=a.gen3_csv,cache_dir=a.cache_dir,output_dir=a.output_dir,min_trades=a.min_trades)
if __name__=="__main__": main()
