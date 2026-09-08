from __future__ import annotations

from dataclasses import dataclass

from market.history import PriceBar
from market.regime import MarketRegime, RegimeDetector, RegimeSnapshot
from research.learning import LearningAssessment, LearningEngine, LearningStatus
from research.performance import PerformanceEvidence, StrategyPerformanceStore
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
    regime_bonus: float = 0.0
    evidence_bonus: float = 0.0
    evidence: PerformanceEvidence | None = None
    learning: LearningAssessment | None = None


@dataclass(frozen=True)
class StrategySelection:
    regime: RegimeSnapshot
    selected: StrategyEvaluation | None
    evaluations: tuple[StrategyEvaluation, ...]
    reason: str


class StrategySelector:
    """Evaluate all strategies and blend signal, regime and bounded learned evidence."""

    def __init__(
        self,
        minimum_score: float = 55.0,
        performance_store: StrategyPerformanceStore | None = None,
    ) -> None:
        self.minimum_score = minimum_score
        self.regime_detector = RegimeDetector()
        self.strategies = [factory() for factory in SINGLE_ASSET_STRATEGIES]
        self.performance_store = performance_store
        self.learning_engine = LearningEngine(performance_store) if performance_store else None

    def evaluate(
        self,
        bars: list[PriceBar],
        *,
        symbol: str = "",
        asset_class: str = "STK",
        timeframe: str = "1 hour",
    ) -> StrategySelection:
        regime = self.regime_detector.evaluate(bars)
        evaluations: list[StrategyEvaluation] = []
        for strategy in self.strategies:
            signal = strategy.evaluate(bars)
            regime_bonus = REGIME_BONUS.get(regime.regime, {}).get(strategy.name, 0.0)
            learning = None
            evidence = None
            evidence_bonus = 0.0
            if self.learning_engine is not None and symbol:
                learning = self.learning_engine.assess(
                    symbol=symbol,
                    asset_class=asset_class,
                    timeframe=timeframe,
                    regime=regime.regime.value,
                    strategy=strategy.name,
                )
                evidence = learning.evidence
                evidence_bonus = learning.selector_bonus

            adjusted = max(0.0, min(100.0, signal.score + regime_bonus + evidence_bonus))
            evaluations.append(StrategyEvaluation(
                strategy.name, signal, adjusted, regime_bonus, evidence_bonus, evidence, learning
            ))

        evaluations.sort(key=lambda x: x.adjusted_score, reverse=True)
        tradable = [x for x in evaluations if x.signal.side != SignalSide.FLAT]
        if not tradable:
            return StrategySelection(regime, None, tuple(evaluations), "all_strategies_flat")

        # Strong negative validated evidence can veto a strategy even if today's raw
        # signal is attractive. Unknown/developing evidence never blocks by itself.
        tradable = [
            x for x in tradable
            if x.learning is None or x.learning.status != LearningStatus.AVOID
        ]
        if not tradable:
            return StrategySelection(regime, None, tuple(evaluations), "all_tradable_strategies_avoided")

        best = tradable[0]
        if best.adjusted_score < self.minimum_score:
            return StrategySelection(regime, None, tuple(evaluations), "best_signal_below_threshold")
        reason = (
            "best_learned_context_signal"
            if best.learning is not None and best.learning.evidence is not None
            else "best_context_adjusted_signal"
        )
        return StrategySelection(regime, best, tuple(evaluations), reason)
