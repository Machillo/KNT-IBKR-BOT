from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from engine.strategy_selector import StrategySelection, StrategySelector
from market.history import HistoricalDataService, PriceBar
from research.performance import StrategyPerformanceStore
from strategies.library import PairSignal, PairsTradingStrategy
from strategies.momentum import SignalSide
from utils.logger import logger


@dataclass(frozen=True)
class ShadowDecision:
    symbol: str
    liquidity_score: float
    selection: StrategySelection
    action: str


@dataclass(frozen=True)
class ShadowPairDecision:
    symbol_a: str
    symbol_b: str
    signal: PairSignal
    action: str


class ShadowTradingEngine:
    """Evaluates discovered candidates with all strategy families; never sends an order."""

    def __init__(self, ib, market_intelligence, minimum_signal_score: float = 55.0,
                 max_candidates: int = 5) -> None:
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.performance_store = StrategyPerformanceStore()
        self.selector = StrategySelector(minimum_signal_score, self.performance_store)
        self.pairs = PairsTradingStrategy()
        self.max_candidates = max_candidates

    async def run_once(self, rows: int = 10) -> list[ShadowDecision]:
        ranked = await self.intelligence.ranked_us_stocks(rows)
        candidates = [x for x in ranked if x.eligible][:self.max_candidates]
        decisions: list[ShadowDecision] = []
        history_by_symbol: dict[str, list[PriceBar]] = {}

        for candidate in candidates:
            try:
                bars = await self.history.bars(candidate.contract)
                history_by_symbol[candidate.symbol] = bars
                selection = self.selector.evaluate(
                    bars,
                    symbol=candidate.symbol,
                    asset_class=candidate.contract.secType or "STK",
                    timeframe="1 hour",
                )
                selected = selection.selected
                action = "NO_TRADE" if selected is None else f"WOULD_{selected.signal.side.value}"
                decision = ShadowDecision(candidate.symbol, candidate.score, selection, action)
                decisions.append(decision)
                top = [
                    f"{item.strategy}:{item.signal.side.value}:{item.adjusted_score:.1f}:hist={item.evidence_bonus:+.1f}"
                    for item in selection.evaluations[:3]
                ]
                logger.info(
                    "SHADOW DECISION | symbol=%s liquidity=%.2f regime=%s action=%s selected=%s top=%s reason=%s",
                    candidate.symbol,
                    candidate.score,
                    selection.regime.regime.value,
                    action,
                    None if selected is None else selected.strategy,
                    top,
                    selection.reason,
                )
                if selected is not None:
                    s = selected.signal
                    logger.info(
                        "SHADOW SETUP | symbol=%s strategy=%s side=%s score=%.2f evidence_bonus=%+.2f entry=%s stop=%s target=%s signal_reason=%s",
                        candidate.symbol, selected.strategy, s.side.value,
                        selected.adjusted_score, selected.evidence_bonus,
                        s.entry, s.stop, s.target, s.reason,
                    )
            except Exception as exc:
                logger.warning("SHADOW candidate skipped | symbol=%s error=%s", candidate.symbol, exc)

        self._evaluate_pairs(history_by_symbol)
        return decisions

    def _evaluate_pairs(self, histories: dict[str, list[PriceBar]]) -> list[ShadowPairDecision]:
        results: list[ShadowPairDecision] = []
        for a, b in combinations(histories, 2):
            signal = self.pairs.evaluate_pair(histories[a], histories[b])
            action = "NO_TRADE"
            if signal.side_a != SignalSide.FLAT and signal.score >= 70:
                action = f"WOULD_PAIR_{signal.side_a.value}_{a}_{signal.side_b.value}_{b}"
            results.append(ShadowPairDecision(a, b, signal, action))
            if action != "NO_TRADE":
                logger.info(
                    "SHADOW PAIR | pair=%s/%s strategy=%s z=%.2f score=%.2f action=%s reason=%s",
                    a, b, self.pairs.name, signal.zscore, signal.score, action, signal.reason,
                )
        return results
