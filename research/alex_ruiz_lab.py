from __future__ import annotations

from dataclasses import dataclass

from market.history import PriceBar
from strategies.momentum import SignalSide, StrategySignal


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _atr(bars: list[PriceBar], period: int = 14) -> float:
    sample = bars[-(period + 1):]
    if len(sample) < 2:
        return 0.0
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(sample, sample[1:])
    ]
    return _mean(trs)


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    changes = [b - a for a, b in zip(closes[-period - 1:-1], closes[-period:])]
    gains = _mean([max(x, 0.0) for x in changes])
    losses = _mean([max(-x, 0.0) for x in changes])
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _flat(reason: str, score: float = 0.0) -> StrategySignal:
    return StrategySignal(SignalSide.FLAT, max(0.0, min(100.0, score)), None, None, None, reason)


def _directional(side: SignalSide, score: float, entry: float, stop: float, target: float, reason: str) -> StrategySignal:
    if min(entry, stop, target) <= 0:
        return _flat("invalid_prices")
    if side == SignalSide.LONG and not (stop < entry < target):
        return _flat("invalid_long_geometry")
    if side == SignalSide.SHORT and not (target < entry < stop):
        return _flat("invalid_short_geometry")
    return StrategySignal(side, max(0.0, min(100.0, score)), entry, stop, target, reason)


@dataclass(frozen=True)
class FibTrendParams:
    swing_lookback: int
    fib_near: float
    fib_far: float
    rr: float
    rsi_padding: int


class FibTrendSweepStrategy:
    warmup = 90

    def __init__(self, params: FibTrendParams) -> None:
        self.params = params
        self.name = (
            f"fibtrend_lb{params.swing_lookback}_"
            f"f{int(params.fib_near*1000)}_{int(params.fib_far*1000)}_"
            f"rr{int(params.rr*10)}_r{params.rsi_padding}"
        )

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        p = self.params
        if len(bars) < max(self.warmup, p.swing_lookback + 2):
            return _flat("insufficient_history")
        closes = [x.close for x in bars]
        price = closes[-1]
        fast = _mean(closes[-20:])
        slow = _mean(closes[-50:])
        a = _atr(bars)
        if a <= 0 or slow <= 0:
            return _flat("invalid_indicators")

        prior = bars[-(p.swing_lookback + 1):-1]
        hi = max(x.high for x in prior)
        lo = min(x.low for x in prior)
        span = hi - lo
        if span <= 0:
            return _flat("invalid_swing")

        rv = _rsi(closes)
        lower_up = hi - p.fib_far * span
        upper_up = hi - p.fib_near * span
        lower_down = lo + p.fib_near * span
        upper_down = lo + p.fib_far * span
        trend_up = fast > slow and price > slow
        trend_down = fast < slow and price < slow
        low_rsi = 42 - p.rsi_padding
        high_rsi = 62 + p.rsi_padding

        if trend_up and lower_up <= price <= upper_up and low_rsi <= rv <= high_rsi:
            stop = min(lo, price - 1.25 * a)
            risk = price - stop
            if risk > 0:
                score = 58 + min(24, (fast - slow) / slow * 3500)
                return _directional(SignalSide.LONG, score, price, stop, price + p.rr * risk, "fib_trend_long")

        if trend_down and lower_down <= price <= upper_down and (38 - p.rsi_padding) <= rv <= (58 + p.rsi_padding):
            stop = max(hi, price + 1.25 * a)
            risk = stop - price
            if risk > 0:
                score = 58 + min(24, (slow - fast) / slow * 3500)
                return _directional(SignalSide.SHORT, score, price, stop, max(0.01, price - p.rr * risk), "fib_trend_short")

        return _flat("fib_trend_absent", 18)


@dataclass(frozen=True)
class LiquidityFibParams:
    structure_lookback: int
    sweep_lookback: int
    fib_threshold: float
    rr: float
    rsi_edge: int


class LiquidityFibSweepStrategy:
    warmup = 90

    def __init__(self, params: LiquidityFibParams) -> None:
        self.params = params
        self.name = (
            f"liqfib_s{params.structure_lookback}_w{params.sweep_lookback}_"
            f"f{int(params.fib_threshold*1000)}_rr{int(params.rr*10)}_r{params.rsi_edge}"
        )

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        p = self.params
        needed = max(self.warmup, p.structure_lookback + 2, p.sweep_lookback + 2)
        if len(bars) < needed:
            return _flat("insufficient_history")
        cur = bars[-1]
        price = cur.close
        a = _atr(bars)
        if a <= 0:
            return _flat("invalid_atr")

        structure = bars[-(p.structure_lookback + 1):-1]
        prior = bars[-(p.sweep_lookback + 1):-1]
        hi = max(x.high for x in structure)
        lo = min(x.low for x in structure)
        prior_hi = max(x.high for x in prior)
        prior_lo = min(x.low for x in prior)
        span = hi - lo
        if span <= 0:
            return _flat("invalid_structure")

        discount = lo + p.fib_threshold * span
        premium = lo + (1.0 - p.fib_threshold) * span
        rv = _rsi([x.close for x in bars])
        sellside = cur.low < prior_lo and cur.close > prior_lo
        buyside = cur.high > prior_hi and cur.close < prior_hi

        if sellside and price <= discount and rv < (50 - p.rsi_edge):
            stop = min(cur.low - 0.15 * a, price - a)
            risk = price - stop
            if risk > 0:
                reclaim = (price - prior_lo) / a
                score = 62 + min(26, max(0.0, reclaim) * 18)
                return _directional(SignalSide.LONG, score, price, stop, price + p.rr * risk, "liquidity_discount_long")

        if buyside and price >= premium and rv > (50 + p.rsi_edge):
            stop = max(cur.high + 0.15 * a, price + a)
            risk = stop - price
            if risk > 0:
                reject = (prior_hi - price) / a
                score = 62 + min(26, max(0.0, reject) * 18)
                return _directional(SignalSide.SHORT, score, price, stop, max(0.01, price - p.rr * risk), "liquidity_premium_short")

        return _flat("liquidity_fib_absent", 18)


def candidate_strategies() -> list[object]:
    """Generate a bounded research grid. These variants never alter live risk limits."""
    strategies: list[object] = []
    for lookback in (40, 60):
        for fib_near, fib_far in ((0.382, 0.618), (0.5, 0.618)):
            for rr in (2.0, 2.7, 3.2):
                for padding in (0, 5):
                    strategies.append(FibTrendSweepStrategy(FibTrendParams(lookback, fib_near, fib_far, rr, padding)))
    for struct_lb in (40, 60):
        for sweep_lb in (15, 20):
            for fib in (0.30, 0.382, 0.45):
                for rr in (2.2, 2.7, 3.2):
                    for rsi_edge in (2, 5):
                        strategies.append(LiquidityFibSweepStrategy(LiquidityFibParams(struct_lb, sweep_lb, fib, rr, rsi_edge)))
    return strategies
