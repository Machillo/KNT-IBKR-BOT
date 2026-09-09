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
    adx: float = 0.0
    ema_slope_pct: float = 0.0
    volatility_stress: float = 0.0


class RegimeDetector:
    """Classify trend/range/stress using volatility, ADX and long-EMA slope.

    EMA200 is used whenever the dataset is long enough. Short OOS windows fall back
    to EMA50 so walk-forward evaluation remains meaningful instead of returning
    MIXED solely because a test slice contains fewer than 200 bars.
    """

    warmup = 51

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (period + 1.0)
        result = float(values[0])
        for value in values[1:]:
            result = alpha * float(value) + (1.0 - alpha) * result
        return result

    @classmethod
    def _ema_slope(cls, closes: list[float]) -> float:
        period = 200 if len(closes) >= 205 else 50
        lookback = 5
        current = cls._ema(closes[-max(period * 2, period + lookback):], period)
        previous_sample = closes[:-lookback]
        if len(previous_sample) < period:
            return 0.0
        previous = cls._ema(previous_sample[-max(period * 2, period):], period)
        return (current / previous - 1.0) if previous > 0 else 0.0

    @staticmethod
    def _adx(bars: list[PriceBar], period: int = 14) -> float:
        if len(bars) < period + 2:
            return 0.0
        sample = bars[-(period + 1):]
        tr_sum = plus_sum = minus_sum = 0.0
        for prev, cur in zip(sample, sample[1:]):
            tr_sum += max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
            up = cur.high - prev.high
            down = prev.low - cur.low
            plus_sum += up if up > down and up > 0 else 0.0
            minus_sum += down if down > up and down > 0 else 0.0
        if tr_sum <= 0:
            return 0.0
        plus_di = 100.0 * plus_sum / tr_sum
        minus_di = 100.0 * minus_sum / tr_sum
        denom = plus_di + minus_di
        return 0.0 if denom <= 0 else 100.0 * abs(plus_di - minus_di) / denom

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
        adx = self._adx(bars)
        ema_slope = self._ema_slope(closes)

        recent_atr = []
        for end in range(max(51, len(bars) - 20), len(bars) + 1):
            sample = bars[:end]
            if sample[-1].close > 0:
                recent_atr.append(atr(sample) / sample[-1].close)
        baseline_vol = mean(recent_atr[:-1]) if len(recent_atr) > 1 else atr_pct
        volatility_stress = atr_pct / baseline_vol if baseline_vol > 0 else 1.0

        if atr_pct >= 0.035 or volatility_stress >= 1.8:
            return RegimeSnapshot(
                MarketRegime.HIGH_VOLATILITY, trend * 100, atr_pct * 100,
                "volatility_stress", adx, ema_slope * 100, volatility_stress,
            )
        if adx >= 25.0 and (trend >= 0.015 or abs(ema_slope) >= 0.004):
            return RegimeSnapshot(
                MarketRegime.TRENDING, trend * 100, atr_pct * 100,
                "adx_and_ema_trend", adx, ema_slope * 100, volatility_stress,
            )
        if adx < 20.0 and trend <= 0.012 and atr_pct <= 0.025:
            return RegimeSnapshot(
                MarketRegime.RANGE, trend * 100, atr_pct * 100,
                "low_adx_low_trend", adx, ema_slope * 100, volatility_stress,
            )
        return RegimeSnapshot(
            MarketRegime.MIXED, trend * 100, atr_pct * 100,
            "no_dominant_regime", adx, ema_slope * 100, volatility_stress,
        )
