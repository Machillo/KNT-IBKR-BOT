from __future__ import annotations

from dataclasses import dataclass

from market.history import PriceBar
from market.regime import MarketRegime, RegimeDetector, RegimeSnapshot
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import SignalSide, StrategySignal


REGIME_BONUS: dict[MarketRegime, dict[str, float]] = {
    MarketRegime.TRENDING: {
        "trend_following_v1": 12.0,
        "momentum_gap_v1": 8.0,
        "swing_structure_v1": 6.0,
        "breakout_v1": 4.0,
    },
    MarketRegime.RANGE: {
        "mean_reversion_v1": 12.0,
        "range_v1": 12.0,
        "pairs_market_neutral_v1": 8.0,
        "smc_liquidity_v1": 4.0,
    },
    MarketRegime.HIGH_VOLATILITY: {
        "breakout_v1": 10.0,
        "momentum_gap_v1": 8.0,
        "smc_liquidity_v1": 6.0,
    },
    MarketRegime.MIXED: {},
}


@dataclass(frozen=True)
class StrategyEvaluation:
    strategy: str
    signal: StrategySignal
    adjusted_score: float


@dataclass(frozen=True)
class StrategySelection:
    regime: RegimeSnapshot
    selected: StrategyEvaluation | None
    evaluations: tuple[StrategyEvaluation, ...]
    reason: str


class StrategySelector:
    """Evaluates every applicable single-asset strategy and chooses the strongest setup.

    Regime preferences are soft bonuses, not hard filters. Historical performance evidence
    will be layered into adjusted_score as the next stage; this selector already avoids
    permanently assigning one strategy to one ticker.
    """

    def __init__(self, minimum_score: float = 55.0) -> None:
        self.minimum_score = minimum_score
        self.regime_detector = RegimeDetector()
        self.strategies = [factory() for factory in SINGLE_ASSET_STRATEGIES]

    def evaluate(self, bars: list[PriceBar]) -> StrategySelection:
        regime = self.regime_detector.evaluate(bars)
        evaluations: list[StrategyEvaluation] = []
        for strategy in self.strategies:
            signal = strategy.evaluate(bars)
            bonus = REGIME_BONUS.get(regime.regime, {}).get(strategy.name, 0.0)
            adjusted = min(100.0, signal.score + bonus)
            evaluations.append(StrategyEvaluation(strategy.name, signal, adjusted))

        evaluations.sort(key=lambda x: x.adjusted_score, reverse=True)
        tradable = [x for x in evaluations if x.signal.side != SignalSide.FLAT]
        if not tradable:
            return StrategySelection(regime, None, tuple(evaluations), "all_strategies_flat")
        best = tradable[0]
        if best.adjusted_score < self.minimum_score:
            return StrategySelection(regime, None, tuple(evaluations), "best_signal_below_threshold")
        return StrategySelection(regime, best, tuple(evaluations), "best_context_adjusted_signal")
