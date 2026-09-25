"""Null models: does the selector beat random entries under identical constraints?

``RandomLongStrategy`` emits a long signal with probability ``p`` on each completed
bar, using the same ATR bracket geometry as the library strategies (stop 1.5 ATR,
target 2.5 ATR). Run through ``PipelineBacktest`` it shares sizing, portfolio caps,
regime pause and costs with the real selector, so the difference isolates the
information content of the signals. Diagnostic only — never a candidate.
"""
from __future__ import annotations

from random import Random

from strategies.library import atr, directional, flat
from strategies.momentum import SignalSide, StrategySignal


class RandomLongStrategy:
    warmup = 31

    def __init__(self, seed: int, p: float = 0.1) -> None:
        self.name = f"random_long_s{seed}"
        self.rng = Random(seed)
        self.p = p

    def evaluate(self, bars) -> StrategySignal:
        if len(bars) < self.warmup:
            return flat("insufficient_history")
        if self.rng.random() >= self.p:
            return flat("random_skip")
        return directional(SignalSide.LONG, 99.0, bars[-1].close, atr(bars), "random_long")
