from __future__ import annotations

from market.history import PriceBar
from strategies.momentum import SignalSide, StrategySignal


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return (sum((x - m) ** 2 for x in values) / (len(values) - 1)) ** 0.5


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


def _directional(
    side: SignalSide,
    score: float,
    entry: float,
    stop: float,
    target: float,
    reason: str,
) -> StrategySignal:
    if min(entry, stop, target) <= 0:
        return _flat("invalid_prices")
    if side == SignalSide.LONG and not (stop < entry < target):
        return _flat("invalid_long_geometry")
    if side == SignalSide.SHORT and not (target < entry < stop):
        return _flat("invalid_short_geometry")
    return StrategySignal(side, max(0.0, min(100.0, score)), entry, stop, target, reason)


def _swing_bounds(bars: list[PriceBar], lookback: int = 30) -> tuple[float, float]:
    recent = bars[-lookback:]
    return max(x.high for x in recent), min(x.low for x in recent)


class FibonacciTrendPullbackStrategy:
    """Trend + Fibonacci pullback + momentum confirmation."""

    name = "fib_trend_pullback_v1"
    warmup = 80

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars) < self.warmup:
            return _flat("insufficient_history")

        closes = [x.close for x in bars]
        price = closes[-1]
        fast = _mean(closes[-20:])
        slow = _mean(closes[-50:])
        a = _atr(bars)
        if a <= 0 or slow <= 0:
            return _flat("invalid_indicators")

        prior = bars[-61:-1]
        swing_high = max(x.high for x in prior)
        swing_low = min(x.low for x in prior)
        span = swing_high - swing_low
        if span <= 0:
            return _flat("invalid_swing")

        rsi = _rsi(closes)
        avg_vol = _mean([x.volume for x in bars[-21:-1]])
        volume_ratio = bars[-1].volume / avg_vol if avg_vol > 0 else 1.0

        trend_up = fast > slow and price > slow
        trend_down = fast < slow and price < slow
        fib_382_up = swing_high - 0.382 * span
        fib_618_up = swing_high - 0.618 * span
        fib_382_down = swing_low + 0.382 * span
        fib_618_down = swing_low + 0.618 * span

        if trend_up and fib_618_up <= price <= fib_382_up and 42 <= rsi <= 62:
            stop = min(swing_low, price - 1.25 * a)
            risk = price - stop
            if risk <= 0:
                return _flat("invalid_risk")
            score = 58 + min(18, (fast - slow) / slow * 3000) + min(12, max(0.0, volume_ratio - 1.0) * 12)
            return _directional(
                SignalSide.LONG,
                score,
                price,
                stop,
                price + 2.4 * risk,
                "uptrend_fib_382_618_pullback_confluence",
            )

        if trend_down and fib_382_down <= price <= fib_618_down and 38 <= rsi <= 58:
            stop = max(swing_high, price + 1.25 * a)
            risk = stop - price
            if risk <= 0:
                return _flat("invalid_risk")
            score = 58 + min(18, (slow - fast) / slow * 3000) + min(12, max(0.0, volume_ratio - 1.0) * 12)
            return _directional(
                SignalSide.SHORT,
                score,
                price,
                stop,
                max(0.01, price - 2.4 * risk),
                "downtrend_fib_382_618_pullback_confluence",
            )

        return _flat("fib_pullback_confluence_absent", 20)


class LiquidityFibReversalStrategy:
    """Liquidity sweep + reclaim/rejection + Fibonacci location + RSI filter."""

    name = "liquidity_fib_reversal_v1"
    warmup = 70

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars) < self.warmup:
            return _flat("insufficient_history")

        cur = bars[-1]
        price = cur.close
        a = _atr(bars)
        if a <= 0:
            return _flat("invalid_atr")

        structure = bars[-51:-1]
        prior = bars[-21:-1]
        struct_hi = max(x.high for x in structure)
        struct_lo = min(x.low for x in structure)
        prior_hi = max(x.high for x in prior)
        prior_lo = min(x.low for x in prior)
        span = struct_hi - struct_lo
        if span <= 0:
            return _flat("invalid_structure")

        fib_618_from_low = struct_lo + 0.618 * span
        fib_382_from_low = struct_lo + 0.382 * span
        rsi = _rsi([x.close for x in bars])

        sellside_sweep = cur.low < prior_lo and cur.close > prior_lo
        buyside_sweep = cur.high > prior_hi and cur.close < prior_hi

        if sellside_sweep and price <= fib_382_from_low and rsi < 48:
            stop = min(cur.low - 0.15 * a, price - 1.0 * a)
            risk = price - stop
            reclaim_strength = (price - prior_lo) / a
            score = 62 + min(22, max(0.0, reclaim_strength) * 18) + min(10, max(0.0, 48 - rsi) * 0.5)
            return _directional(
                SignalSide.LONG,
                score,
                price,
                stop,
                price + 2.7 * risk,
                "sellside_sweep_discount_fib_reclaim",
            )

        if buyside_sweep and price >= fib_618_from_low and rsi > 52:
            stop = max(cur.high + 0.15 * a, price + 1.0 * a)
            risk = stop - price
            rejection_strength = (prior_hi - price) / a
            score = 62 + min(22, max(0.0, rejection_strength) * 18) + min(10, max(0.0, rsi - 52) * 0.5)
            return _directional(
                SignalSide.SHORT,
                score,
                price,
                stop,
                max(0.01, price - 2.7 * risk),
                "buyside_sweep_premium_fib_rejection",
            )

        return _flat("liquidity_fib_confluence_absent", 18)


class StructureSRConfluenceStrategy:
    """Market structure + dynamic support/resistance + trend/volume confirmation."""

    name = "structure_sr_confluence_v1"
    warmup = 80

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        if len(bars) < self.warmup:
            return _flat("insufficient_history")

        closes = [x.close for x in bars]
        price = closes[-1]
        a = _atr(bars)
        if a <= 0:
            return _flat("invalid_atr")

        fast = _mean(closes[-20:])
        slow = _mean(closes[-50:])
        recent = bars[-40:]
        first = recent[:20]
        second = recent[20:]

        first_hi, first_lo = max(x.high for x in first), min(x.low for x in first)
        second_hi, second_lo = max(x.high for x in second), min(x.low for x in second)
        bullish_structure = second_hi > first_hi and second_lo > first_lo
        bearish_structure = second_hi < first_hi and second_lo < first_lo

        support = min(x.low for x in bars[-20:])
        resistance = max(x.high for x in bars[-20:])
        avg_vol = _mean([x.volume for x in bars[-21:-1]])
        volume_ratio = bars[-1].volume / avg_vol if avg_vol > 0 else 1.0
        rsi = _rsi(closes)

        near_support = (price - support) <= 1.25 * a
        near_resistance = (resistance - price) <= 1.25 * a

        if bullish_structure and fast > slow and near_support and rsi >= 45:
            stop = support - 0.35 * a
            risk = price - stop
            if risk <= 0:
                return _flat("invalid_risk")
            score = 60 + min(15, max(0.0, volume_ratio - 1.0) * 15) + min(15, max(0.0, rsi - 45) * 0.6)
            return _directional(
                SignalSide.LONG,
                score,
                price,
                stop,
                price + 2.5 * risk,
                "bullish_structure_support_trend_confluence",
            )

        if bearish_structure and fast < slow and near_resistance and rsi <= 55:
            stop = resistance + 0.35 * a
            risk = stop - price
            if risk <= 0:
                return _flat("invalid_risk")
            score = 60 + min(15, max(0.0, volume_ratio - 1.0) * 15) + min(15, max(0.0, 55 - rsi) * 0.6)
            return _directional(
                SignalSide.SHORT,
                score,
                price,
                stop,
                max(0.01, price - 2.5 * risk),
                "bearish_structure_resistance_trend_confluence",
            )

        return _flat("structure_sr_confluence_absent", 20)
