from __future__ import annotations

from dataclasses import dataclass
from backtest.engine import BacktestEngine
from backtest.monthly_target import summarize_months
from market.history import PriceBar
from research.confluence_lab import candidate_strategies

@dataclass(frozen=True)
class Gen3Result:
    symbol:str; profile:str; strategy:str; train_monthly_pct:float; validation_monthly_pct:float; test_monthly_pct:float; train_dd_pct:float; validation_dd_pct:float; test_dd_pct:float; test_positive_month_rate_pct:float; test_trades:int

def split_train_validation_test(bars:list[PriceBar])->tuple[list[PriceBar],list[PriceBar],list[PriceBar]]:
    if len(bars)<300:return [],[],[]
    train_end=int(len(bars)*.60); validation_end=int(len(bars)*.80)
    return bars[:train_end],bars[train_end:validation_end],bars[validation_end:]

def _run(bars:list[PriceBar],strategy:object):
    return BacktestEngine(initial_equity=10_000.0,risk_pct=.01,commission_bps=2.0,slippage_bps=2.0,max_position_pct=.10).run(bars,strategy)

def _selection_score(result)->float:
    monthly=summarize_months(result); pf=result.profit_factor
    finite_pf=0.0 if pf is None else min(3.0,3.0 if pf==float("inf") else float(pf))
    return monthly.compounded_monthly_pct*4.0+monthly.positive_month_rate_pct*.08+finite_pf*2.0-result.max_drawdown_pct*.75

def select_and_test(symbol:str,profile:str,bars:list[PriceBar])->Gen3Result|None:
    train,validation,test=split_train_validation_test(bars)
    if not train or not validation or not test:return None
    survivors=[]
    for strategy in candidate_strategies():
        trr=_run(train,strategy); tr=summarize_months(trr)
        if trr.trades<8 or tr.compounded_monthly_pct<=0 or trr.max_drawdown_pct>20:continue
        var=_run(validation,strategy); va=summarize_months(var)
        if var.trades<3 or va.compounded_monthly_pct<=0 or var.max_drawdown_pct>20:continue
        survivors.append((.35*_selection_score(trr)+.65*_selection_score(var),strategy,trr,var))
    if not survivors:return None
    survivors.sort(key=lambda x:x[0],reverse=True); _,winner,trr,var=survivors[0]
    ter=_run(test,winner); tr=summarize_months(trr); va=summarize_months(var); te=summarize_months(ter)
    return Gen3Result(symbol,profile,str(winner.name),tr.compounded_monthly_pct,va.compounded_monthly_pct,te.compounded_monthly_pct,trr.max_drawdown_pct,var.max_drawdown_pct,ter.max_drawdown_pct,te.positive_month_rate_pct,ter.trades)
