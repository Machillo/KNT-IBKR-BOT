from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from market.history import PriceBar
from strategies.confluence import (
    FibonacciTrendPullbackStrategy,
    LiquidityFibReversalStrategy,
    StructureSRConfluenceStrategy,
)
from strategies.momentum import SignalSide, StrategySignal


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def atr(bars: list[PriceBar], period: int = 14) -> float:
    sample = bars[-(period + 1):]
    trs = [max(cur.high-cur.low, abs(cur.high-prev.close), abs(cur.low-prev.close)) for prev, cur in zip(sample, sample[1:])]
    return mean(trs)


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes = [b-a for a, b in zip(closes[-period-1:-1], closes[-period:])]
    gains = mean([max(x, 0.0) for x in changes])
    losses = mean([max(-x, 0.0) for x in changes])
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def flat(reason: str, score: float = 0.0) -> StrategySignal:
    return StrategySignal(SignalSide.FLAT, score, None, None, None, reason)


def directional(side: SignalSide, score: float, price: float, volatility: float, reason: str,
                stop_mult: float = 1.5, target_mult: float = 2.5) -> StrategySignal:
    if volatility <= 0:
        return flat("invalid_volatility")
    if side == SignalSide.LONG:
        return StrategySignal(side, min(100.0, score), price, max(0.01, price-stop_mult*volatility), price+target_mult*volatility, reason)
    return StrategySignal(side, min(100.0, score), price, price+stop_mult*volatility, max(0.01, price-target_mult*volatility), reason)


class BreakoutStrategy:
    name = "breakout_v1"
    warmup = 31
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars) < self.warmup: return flat("insufficient_history")
        lookback = bars[-21:-1]; price = bars[-1].close; a = atr(bars)
        hi, lo = max(x.high for x in lookback), min(x.low for x in lookback)
        avg_vol = mean([x.volume for x in lookback]); vol_ratio = bars[-1].volume / avg_vol if avg_vol > 0 else 1.0
        if price > hi: return directional(SignalSide.LONG, 55 + min(45, max(0, vol_ratio-1)*30), price, a, "20bar_high_breakout")
        if price < lo: return directional(SignalSide.SHORT, 55 + min(45, max(0, vol_ratio-1)*30), price, a, "20bar_low_breakout")
        return flat("inside_breakout_range", max(0, 40-abs(price-(hi+lo)/2)/max(a, .01)))


class MomentumGapStrategy:
    name = "momentum_gap_v1"
    warmup = 31
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars) < self.warmup: return flat("insufficient_history")
        closes=[x.close for x in bars]; price=closes[-1]; a=atr(bars)
        m5=(price/closes[-6]-1) if closes[-6] else 0; m20=(price/closes[-21]-1) if closes[-21] else 0
        gap=(bars[-1].open/bars[-2].close-1) if bars[-2].close else 0
        strength=(abs(m5)*3500 + abs(m20)*1500 + abs(gap)*2500)
        if m5>0 and m20>0 and gap>-0.02: return directional(SignalSide.LONG, strength, price, a, "positive_multi_horizon_momentum")
        if m5<0 and m20<0 and gap<0.02: return directional(SignalSide.SHORT, strength, price, a, "negative_multi_horizon_momentum")
        return flat("momentum_not_aligned", min(100, strength))


class SwingStructureStrategy:
    name = "swing_structure_v1"
    warmup = 25
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars)<self.warmup: return flat("insufficient_history")
        a=atr(bars); price=bars[-1].close
        recent=bars[-20:]; first=recent[:10]; second=recent[10:]
        hh=max(x.high for x in second)>max(x.high for x in first); hl=min(x.low for x in second)>min(x.low for x in first)
        lh=max(x.high for x in second)<max(x.high for x in first); ll=min(x.low for x in second)<min(x.low for x in first)
        if hh and hl: return directional(SignalSide.LONG, 70, price, a, "higher_high_higher_low")
        if lh and ll: return directional(SignalSide.SHORT, 70, price, a, "lower_high_lower_low")
        return flat("mixed_market_structure", 25)


class TrendFollowingStrategy:
    name = "trend_following_v1"
    warmup = 51
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars)<self.warmup: return flat("insufficient_history")
        closes=[x.close for x in bars]; price=closes[-1]; fast=mean(closes[-20:]); slow=mean(closes[-50:]); a=atr(bars)
        separation=abs(fast-slow)/slow if slow else 0; score=min(100, 45+separation*5000)
        if fast>slow and price>fast: return directional(SignalSide.LONG, score, price, a, "price_above_20_50_trend")
        if fast<slow and price<fast: return directional(SignalSide.SHORT, score, price, a, "price_below_20_50_trend")
        return flat("trend_not_confirmed", score/2)


class MeanReversionStrategy:
    name = "mean_reversion_v1"
    warmup = 31
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars)<self.warmup: return flat("insufficient_history")
        closes=[x.close for x in bars]; window=closes[-20:]; m=mean(window); sd=stdev(window); price=closes[-1]; a=atr(bars)
        if sd<=0: return flat("zero_dispersion")
        z=(price-m)/sd; rv=rsi(closes)
        score=min(100, abs(z)*30 + abs(rv-50))
        if z<=-1.8 and rv<35: return directional(SignalSide.LONG, score, price, a, "oversold_bollinger_rsi")
        if z>=1.8 and rv>65: return directional(SignalSide.SHORT, score, price, a, "overbought_bollinger_rsi")
        return flat("not_statistically_extended", score)


class RangeTradingStrategy:
    name = "range_v1"
    warmup = 31
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars)<self.warmup: return flat("insufficient_history")
        recent=bars[-20:]; hi=max(x.high for x in recent); lo=min(x.low for x in recent); price=bars[-1].close; a=atr(bars)
        width=hi-lo
        if width<=0: return flat("invalid_range")
        pos=(price-lo)/width; slope=abs(mean([x.close for x in recent[-5:]])-mean([x.close for x in recent[:5]]))/max(price,.01)
        if slope>0.08: return flat("range_not_stable", 10)
        rv=rsi([x.close for x in bars])
        if pos<0.18 and rv<45: return directional(SignalSide.LONG, 65+(0.18-pos)*100, price, a, "near_range_support")
        if pos>0.82 and rv>55: return directional(SignalSide.SHORT, 65+(pos-0.82)*100, price, a, "near_range_resistance")
        return flat("middle_of_range", 20)


class SmartMoneyLiquidityStrategy:
    name = "smc_liquidity_v1"
    warmup = 31
    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars)<self.warmup: return flat("insufficient_history")
        prev=bars[-21:-1]; cur=bars[-1]; price=cur.close; a=atr(bars)
        prior_hi=max(x.high for x in prev); prior_lo=min(x.low for x in prev)
        if cur.low < prior_lo and cur.close > prior_lo:
            reclaim=(cur.close-prior_lo)/max(a,.01)
            return directional(SignalSide.LONG, 60+min(40,reclaim*25), price, a, "sellside_liquidity_sweep_reclaim")
        if cur.high > prior_hi and cur.close < prior_hi:
            reject=(prior_hi-cur.close)/max(a,.01)
            return directional(SignalSide.SHORT, 60+min(40,reject*25), price, a, "buyside_liquidity_sweep_rejection")
        return flat("no_quantified_liquidity_sweep", 15)


@dataclass(frozen=True)
class PairSignal:
    side_a: SignalSide
    side_b: SignalSide
    score: float
    zscore: float
    reason: str


class PairsTradingStrategy:
    name = "pairs_market_neutral_v1"
    warmup = 60
    def evaluate_pair(self, bars_a: list[PriceBar], bars_b: list[PriceBar]) -> PairSignal:
        n=min(len(bars_a),len(bars_b))
        if n<self.warmup: return PairSignal(SignalSide.FLAT, SignalSide.FLAT, 0, 0, "insufficient_history")
        a=[x.close for x in bars_a[-60:]]; b=[x.close for x in bars_b[-60:]]
        ratios=[x/y for x,y in zip(a,b) if y>0]
        if len(ratios)<self.warmup: return PairSignal(SignalSide.FLAT,SignalSide.FLAT,0,0,"invalid_pair_prices")
        m=mean(ratios); sd=stdev(ratios)
        if sd<=0: return PairSignal(SignalSide.FLAT,SignalSide.FLAT,0,0,"zero_spread_dispersion")
        z=(ratios[-1]-m)/sd; score=min(100,abs(z)*35)
        if z>=2: return PairSignal(SignalSide.SHORT,SignalSide.LONG,score,z,"ratio_above_mean")
        if z<=-2: return PairSignal(SignalSide.LONG,SignalSide.SHORT,score,z,"ratio_below_mean")
        return PairSignal(SignalSide.FLAT,SignalSide.FLAT,score,z,"pair_spread_not_extended")


SINGLE_ASSET_STRATEGIES = (
    BreakoutStrategy,
    MomentumGapStrategy,
    SwingStructureStrategy,
    TrendFollowingStrategy,
    MeanReversionStrategy,
    RangeTradingStrategy,
    SmartMoneyLiquidityStrategy,
    FibonacciTrendPullbackStrategy,
    LiquidityFibReversalStrategy,
    StructureSRConfluenceStrategy,
)
