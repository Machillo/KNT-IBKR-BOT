from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import sqrt

from engine.strategy_selector import StrategySelection, StrategySelector
from market.history import HistoricalDataService, PriceBar
from market.session import USStockSessionPolicy
from portfolio.allocation import PortfolioAllocator
from portfolio.brain import PortfolioBrain, PortfolioSnapshot
from research.coordinator import ContinuousResearchCoordinator
from research.performance import StrategyPerformanceStore
from research.scheduler import ContinuousResearchScheduler
from strategies.library import PairSignal, PairsTradingStrategy
from strategies.momentum import SignalSide
from utils.logger import logger


@dataclass(frozen=True)
class ShadowDecision:
    symbol: str
    liquidity_score: float
    selection: StrategySelection
    action: str
    portfolio_reason: str | None = None


@dataclass(frozen=True)
class ShadowPairDecision:
    symbol_a: str
    symbol_b: str
    signal: PairSignal
    action: str


class ShadowTradingEngine:
    """Evaluates dynamic universe candidates with all strategy families; never sends an order."""

    def __init__(self, ib, market_intelligence, minimum_signal_score: float = 55.0,
                 max_candidates: int = 12, quote_budget: int = 40,
                 research_budget: int = 2) -> None:
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.performance_store = StrategyPerformanceStore()
        self.selector = StrategySelector(minimum_signal_score, self.performance_store)
        self.research = ContinuousResearchCoordinator(self.history, self.performance_store)
        self.research_scheduler = ContinuousResearchScheduler(
            self.research, self.performance_store, max_attempts_per_cycle=research_budget
        )
        self.session_policy = USStockSessionPolicy()
        self.portfolio_brain = PortfolioBrain(max_single_position_pct=0.10)
        self.allocator = PortfolioAllocator(risk_pct=0.01, max_position_pct=0.10)
        self.pairs = PairsTradingStrategy()
        self.max_candidates = max_candidates
        self.quote_budget = quote_budget
        self.research_budget = max(0, min(research_budget, max_candidates))

    async def run_once(
        self,
        rows_per_scanner: int = 25,
        *,
        portfolio_snapshot: PortfolioSnapshot | None = None,
    ) -> list[ShadowDecision]:
        ranked = await self.intelligence.ranked_us_opportunity_universe(
            rows_per_plan=rows_per_scanner, quote_budget=self.quote_budget,
        )
        candidates = [x for x in ranked if x.eligible][:self.max_candidates]
        logger.info("SHADOW FUNNEL | ranked=%s eligible=%s deep_analysis=%s",
                    len(ranked), sum(1 for x in ranked if x.eligible), len(candidates))
        decisions: list[ShadowDecision] = []
        history_by_symbol: dict[str, list[PriceBar]] = {}

        session = self.session_policy.state()
        logger.info(
            "MARKET SESSION | asset=US_STOCKS session=%s market_open=%s local=%s",
            session.session, session.market_open, session.local_time.isoformat(),
        )
        await self.research_scheduler.run_cycle(candidates, market_open=session.market_open)

        for candidate in candidates:
            try:
                bars = await self.history.bars(candidate.contract)
                history_by_symbol[candidate.symbol] = bars
                asset_class = candidate.contract.secType or "STK"
                selection = self.selector.evaluate(
                    bars, symbol=candidate.symbol,
                    asset_class=asset_class, timeframe="1 hour",
                )
                selected = selection.selected
                action = "NO_TRADE" if selected is None else f"WOULD_{selected.signal.side.value}"
                portfolio_reason = None

                if selected is not None and portfolio_snapshot is not None:
                    signal = selected.signal
                    if signal.entry is None or signal.stop is None:
                        action = "NO_TRADE"
                        portfolio_reason = "invalid_signal_prices"
                    else:
                        proposal = self.allocator.propose(
                            portfolio_snapshot,
                            symbol=candidate.symbol,
                            asset_class=asset_class,
                            entry_price=float(signal.entry),
                            stop_price=float(signal.stop),
                        )
                        if proposal is None:
                            action = "PORTFOLIO_REJECTED"
                            portfolio_reason = "allocation_unavailable"
                        else:
                            portfolio_decision = self.portfolio_brain.evaluate(
                                portfolio_snapshot, proposal.as_opportunity()
                            )
                            portfolio_reason = portfolio_decision.reason
                            if not portfolio_decision.approved:
                                action = "PORTFOLIO_REJECTED"
                            logger.info(
                                "PORTFOLIO ADMISSION | symbol=%s qty=%.4f notional=%.2f risk=%.2f approved=%s reason=%s projected_exposure=%.2f%%",
                                candidate.symbol, proposal.quantity, proposal.proposed_notional,
                                proposal.proposed_risk, portfolio_decision.approved,
                                portfolio_decision.reason,
                                portfolio_decision.projected_exposure_pct * 100,
                            )

                decisions.append(ShadowDecision(
                    candidate.symbol, candidate.score, selection, action, portfolio_reason
                ))
                top = []
                for item in selection.evaluations[:3]:
                    learning = item.learning
                    learned = "UNKNOWN" if learning is None else f"{learning.status.value}:{learning.confidence:.0f}%"
                    top.append(
                        f"{item.strategy}:{item.signal.side.value}:{item.adjusted_score:.1f}:learn={learned}:bonus={item.evidence_bonus:+.1f}"
                    )
                logger.info(
                    "SHADOW DECISION | symbol=%s liquidity=%.2f regime=%s action=%s selected=%s top=%s reason=%s portfolio_reason=%s",
                    candidate.symbol, candidate.score, selection.regime.regime.value, action,
                    None if selected is None else selected.strategy, top, selection.reason,
                    portfolio_reason,
                )
                if selected is not None:
                    s = selected.signal
                    learning = selected.learning
                    logger.info(
                        "SHADOW SETUP | symbol=%s strategy=%s side=%s score=%.2f learning=%s confidence=%.2f evidence_bonus=%+.2f entry=%s stop=%s target=%s signal_reason=%s",
                        candidate.symbol, selected.strategy, s.side.value, selected.adjusted_score,
                        "UNKNOWN" if learning is None else learning.status.value,
                        0.0 if learning is None else learning.confidence,
                        selected.evidence_bonus, s.entry, s.stop, s.target, s.reason,
                    )
            except Exception as exc:
                logger.warning("SHADOW candidate skipped | symbol=%s error=%s", candidate.symbol, exc)

        self._evaluate_pairs(history_by_symbol)
        return decisions

    @staticmethod
    def _return_series(bars: list[PriceBar], lookback: int = 60) -> list[float]:
        closes = [float(b.close) for b in bars[-(lookback + 1):] if float(b.close) > 0]
        if len(closes) < lookback + 1:
            return []
        return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]

    @classmethod
    def _pair_correlation(cls, a: list[PriceBar], b: list[PriceBar]) -> float | None:
        ra = cls._return_series(a)
        rb = cls._return_series(b)
        n = min(len(ra), len(rb))
        if n < 40:
            return None
        ra, rb = ra[-n:], rb[-n:]
        ma, mb = sum(ra) / n, sum(rb) / n
        va = sum((x - ma) ** 2 for x in ra)
        vb = sum((x - mb) ** 2 for x in rb)
        if va <= 0 or vb <= 0:
            return None
        cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
        return cov / sqrt(va * vb)

    def _evaluate_pairs(self, histories: dict[str, list[PriceBar]]) -> list[ShadowPairDecision]:
        results: list[ShadowPairDecision] = []
        considered = relationship_eligible = 0
        for a, b in combinations(histories, 2):
            considered += 1
            corr = self._pair_correlation(histories[a], histories[b])
            if corr is None or corr < 0.55:
                continue
            relationship_eligible += 1
            signal = self.pairs.evaluate_pair(histories[a], histories[b])
            action = "NO_TRADE"
            if signal.side_a != SignalSide.FLAT and signal.score >= 70:
                action = f"WOULD_PAIR_{signal.side_a.value}_{a}_{signal.side_b.value}_{b}"
            results.append(ShadowPairDecision(a, b, signal, action))
            if action != "NO_TRADE":
                logger.info(
                    "SHADOW PAIR | pair=%s/%s corr=%.2f strategy=%s z=%.2f score=%.2f action=%s reason=%s",
                    a, b, corr, self.pairs.name, signal.zscore, signal.score, action, signal.reason,
                )
        logger.info("SHADOW PAIR FUNNEL | combinations=%s relationship_eligible=%s",
                    considered, relationship_eligible)
        return results
