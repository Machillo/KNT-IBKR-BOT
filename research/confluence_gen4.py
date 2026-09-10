from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class Gen4Candidate:
    symbol:str; profile:str; strategy:str; test_monthly_pct:float; test_dd_pct:float; positive_month_rate_pct:float; trades:int

@dataclass(frozen=True)
class Gen4PortfolioResult:
    candidates:int; months:int; compounded_monthly_pct:float; positive_month_rate_pct:float; max_drawdown_pct:float; final_equity:float

def admit_candidates(rows:list[Gen4Candidate],*,min_trades:int=12,min_positive_month_rate:float=50.0,max_dd_pct:float=10.0)->list[Gen4Candidate]:
    return [r for r in rows if r.trades>=min_trades and r.test_monthly_pct>0 and r.positive_month_rate_pct>=min_positive_month_rate and r.test_dd_pct<=max_dd_pct]

def simulate_equal_risk_portfolio(monthly_returns:dict[str,dict[str,float]],*,initial_equity:float=10_000.0,max_strategy_weight:float=.20,max_gross_weight:float=1.0)->Gen4PortfolioResult:
    if initial_equity<=0:raise ValueError("initial_equity must be positive")
    if not 0<max_strategy_weight<=1 or not 0<max_gross_weight<=1:raise ValueError("invalid portfolio weights")
    months=sorted({m for s in monthly_returns.values() for m in s}); equity=initial_equity; peak=equity; max_dd=0.0; positive=0; used=0
    for month in months:
        active=[s[month] for s in monthly_returns.values() if month in s]
        if not active:continue
        weight=min(max_strategy_weight,max_gross_weight/len(active)); ret=sum((r/100.0)*weight for r in active)
        equity*=1+ret; used+=1; positive+=ret>0; peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak if peak else 0.0)
    compounded=((equity/initial_equity)**(1/used)-1)*100 if used and equity>0 else 0.0
    return Gen4PortfolioResult(len(monthly_returns),used,compounded,positive/used*100 if used else 0.0,max_dd*100,equity)
