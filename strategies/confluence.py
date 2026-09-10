from __future__ import annotations

from market.history import PriceBar
from strategies.momentum import SignalSide, StrategySignal


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _atr(bars: list[PriceBar], period: int = 14) -> float:
    sample = bars[-(period + 1):]
    if len(sample) < 2:
        return 0.0
    trs = [max(cur.high-cur.low, abs(cur.high-prev.close), abs(cur.low-prev.close)) for prev,cur in zip(sample,sample[1:])]
    return _mean(trs)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes=[b-a for a,b in zip(closes[-period-1:-1],closes[-period:])]
    gains=_mean([max(x,0.0) for x in changes]); losses=_mean([max(-x,0.0) for x in changes])
    if losses == 0: return 100.0
    rs=gains/losses
    return 100.0-100.0/(1.0+rs)


def _flat(reason: str, score: float = 0.0) -> StrategySignal:
    return StrategySignal(SignalSide.FLAT,max(0.0,min(100.0,score)),None,None,None,reason)


def _directional(side: SignalSide, score: float, entry: float, stop: float, target: float, reason: str) -> StrategySignal:
    if min(entry,stop,target)<=0: return _flat("invalid_prices")
    if side==SignalSide.LONG and not(stop<entry<target): return _flat("invalid_long_geometry")
    if side==SignalSide.SHORT and not(target<entry<stop): return _flat("invalid_short_geometry")
    return StrategySignal(side,max(0.0,min(100.0,score)),entry,stop,target,reason)


class FibonacciTrendPullbackStrategy:
    name="fib_trend_pullback_v1"; warmup=80
    def evaluate(self,bars:list[PriceBar])->StrategySignal:
        if len(bars)<self.warmup:return _flat("insufficient_history")
        closes=[x.close for x in bars]; price=closes[-1]; fast=_mean(closes[-20:]); slow=_mean(closes[-50:]); a=_atr(bars)
        if a<=0 or slow<=0:return _flat("invalid_indicators")
        prior=bars[-61:-1]; hi=max(x.high for x in prior); lo=min(x.low for x in prior); span=hi-lo
        if span<=0:return _flat("invalid_swing")
        rv=_rsi(closes); avg_vol=_mean([x.volume for x in bars[-21:-1]]); vr=bars[-1].volume/avg_vol if avg_vol>0 else 1.0
        if fast>slow and price>slow and hi-.618*span<=price<=hi-.382*span and 42<=rv<=62:
            stop=min(lo,price-1.25*a); risk=price-stop
            if risk>0:return _directional(SignalSide.LONG,58+min(18,(fast-slow)/slow*3000)+min(12,max(0.0,vr-1)*12),price,stop,price+2.4*risk,"uptrend_fib_382_618_pullback_confluence")
        if fast<slow and price<slow and lo+.382*span<=price<=lo+.618*span and 38<=rv<=58:
            stop=max(hi,price+1.25*a); risk=stop-price
            if risk>0:return _directional(SignalSide.SHORT,58+min(18,(slow-fast)/slow*3000)+min(12,max(0.0,vr-1)*12),price,stop,max(.01,price-2.4*risk),"downtrend_fib_382_618_pullback_confluence")
        return _flat("fib_pullback_confluence_absent",20)


class LiquidityFibReversalStrategy:
    name="liquidity_fib_reversal_v1"; warmup=70
    def evaluate(self,bars:list[PriceBar])->StrategySignal:
        if len(bars)<self.warmup:return _flat("insufficient_history")
        cur=bars[-1]; price=cur.close; a=_atr(bars)
        if a<=0:return _flat("invalid_atr")
        structure=bars[-51:-1]; prior=bars[-21:-1]; hi=max(x.high for x in structure); lo=min(x.low for x in structure); prior_hi=max(x.high for x in prior); prior_lo=min(x.low for x in prior); span=hi-lo
        if span<=0:return _flat("invalid_structure")
        rv=_rsi([x.close for x in bars]); discount=lo+.382*span; premium=lo+.618*span
        if cur.low<prior_lo and cur.close>prior_lo and price<=discount and rv<48:
            stop=min(cur.low-.15*a,price-a); risk=price-stop
            if risk>0:return _directional(SignalSide.LONG,62+min(22,max(0.0,(price-prior_lo)/a)*18)+min(10,max(0.0,48-rv)*.5),price,stop,price+2.7*risk,"sellside_sweep_discount_fib_reclaim")
        if cur.high>prior_hi and cur.close<prior_hi and price>=premium and rv>52:
            stop=max(cur.high+.15*a,price+a); risk=stop-price
            if risk>0:return _directional(SignalSide.SHORT,62+min(22,max(0.0,(prior_hi-price)/a)*18)+min(10,max(0.0,rv-52)*.5),price,stop,max(.01,price-2.7*risk),"buyside_sweep_premium_fib_rejection")
        return _flat("liquidity_fib_confluence_absent",18)


class StructureSRConfluenceStrategy:
    name="structure_sr_confluence_v1"; warmup=80
    def evaluate(self,bars:list[PriceBar])->StrategySignal:
        if len(bars)<self.warmup:return _flat("insufficient_history")
        closes=[x.close for x in bars]; price=closes[-1]; a=_atr(bars)
        if a<=0:return _flat("invalid_atr")
        fast=_mean(closes[-20:]); slow=_mean(closes[-50:]); recent=bars[-40:]; first=recent[:20]; second=recent[20:]
        fhi,flo=max(x.high for x in first),min(x.low for x in first); shi,slo=max(x.high for x in second),min(x.low for x in second)
        support=min(x.low for x in bars[-20:]); resistance=max(x.high for x in bars[-20:]); avg_vol=_mean([x.volume for x in bars[-21:-1]]); vr=bars[-1].volume/avg_vol if avg_vol>0 else 1.0; rv=_rsi(closes)
        if shi>fhi and slo>flo and fast>slow and price-support<=1.25*a and rv>=45:
            stop=support-.35*a; risk=price-stop
            if risk>0:return _directional(SignalSide.LONG,60+min(15,max(0.0,vr-1)*15)+min(15,max(0.0,rv-45)*.6),price,stop,price+2.5*risk,"bullish_structure_support_trend_confluence")
        if shi<fhi and slo<flo and fast<slow and resistance-price<=1.25*a and rv<=55:
            stop=resistance+.35*a; risk=stop-price
            if risk>0:return _directional(SignalSide.SHORT,60+min(15,max(0.0,vr-1)*15)+min(15,max(0.0,55-rv)*.6),price,stop,max(.01,price-2.5*risk),"bearish_structure_resistance_trend_confluence")
        return _flat("structure_sr_confluence_absent",20)
