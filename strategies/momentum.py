from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market.history import PriceBar


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class StrategySignal:
    side: SignalSide
    score: float
    entry: float | None
    stop: float | None
    target: float | None
    reason: str


class MomentumStrategy:
    """Simple, deterministic baseline strategy; intentionally not claimed profitable."""

    name = "momentum_v1"

    def __init__(self, fast: int = 10, slow: int = 30, atr_period: int = 14) -> None:
        if fast < 2 or slow <= fast or atr_period < 2:
            raise ValueError("Invalid momentum parameters")
        self.fast = fast
        self.slow = slow
        self.atr_period = atr_period
        self.warmup = max(self.slow, self.atr_period + 1) + 1

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values)

    def _atr(self, bars: list[PriceBar]) -> float:
        sample = bars[-(self.atr_period + 1):]
        trs: list[float] = []
        for prev, current in zip(sample, sample[1:]):
            trs.append(max(
                current.high - current.low,
                abs(current.high - prev.close),
                abs(current.low - prev.close),
            ))
        return self._mean(trs) if trs else 0.0

    def evaluate(self, bars: list[PriceBar]) -> StrategySignal:
        minimum = self.warmup
        if len(bars) < minimum:
            return StrategySignal(SignalSide.FLAT, 0.0, None, None, None, "insufficient_history")

        closes = [bar.close for bar in bars]
        fast_ma = self._mean(closes[-self.fast:])
        slow_ma = self._mean(closes[-self.slow:])
        previous_slow = self._mean(closes[-self.slow-1:-1])
        price = closes[-1]
        atr = self._atr(bars)
        if atr <= 0 or slow_ma <= 0:
            return StrategySignal(SignalSide.FLAT, 0.0, None, None, None, "invalid_volatility")

        trend_strength = abs(fast_ma - slow_ma) / slow_ma
        score = min(100.0, trend_strength * 10_000)
        if fast_ma > slow_ma and slow_ma > previous_slow and price > fast_ma:
            return StrategySignal(
                SignalSide.LONG, score, price,
                max(0.01, price - 2 * atr), price + 3 * atr,
                "fast_above_rising_slow",
            )
        if fast_ma < slow_ma and slow_ma < previous_slow and price < fast_ma:
            return StrategySignal(
                SignalSide.SHORT, score, price,
                price + 2 * atr, max(0.01, price - 3 * atr),
                "fast_below_falling_slow",
            )
        return StrategySignal(SignalSide.FLAT, score, None, None, None, "no_alignment")
