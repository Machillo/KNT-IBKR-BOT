from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from market.history import PriceBar
from strategies.library import atr, mean


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    MIXED = "MIXED"


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: MarketRegime
    trend_strength_pct: float
    atr_pct: float
    reason: str


class RegimeDetector:
    warmup = 51

    def evaluate(self, bars: list[PriceBar]) -> RegimeSnapshot:
        if len(bars) < self.warmup:
            return RegimeSnapshot(MarketRegime.MIXED, 0.0, 0.0, "insufficient_history")
        closes = [bar.close for bar in bars]
        price = closes[-1]
        fast = mean(closes[-20:])
        slow = mean(closes[-50:])
        a = atr(bars)
        trend = abs(fast - slow) / slow if slow > 0 else 0.0
        atr_pct = a / price if price > 0 else 0.0

        if atr_pct >= 0.035:
            return RegimeSnapshot(MarketRegime.HIGH_VOLATILITY, trend * 100, atr_pct * 100, "atr_expansion")
        if trend >= 0.025:
            return RegimeSnapshot(MarketRegime.TRENDING, trend * 100, atr_pct * 100, "20_50_ma_separation")
        if trend <= 0.008 and atr_pct <= 0.025:
            return RegimeSnapshot(MarketRegime.RANGE, trend * 100, atr_pct * 100, "low_trend_low_volatility")
        return RegimeSnapshot(MarketRegime.MIXED, trend * 100, atr_pct * 100, "no_dominant_regime")
