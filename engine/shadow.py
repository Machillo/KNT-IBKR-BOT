from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import sqrt

from ib_async import Contract

from engine.strategy_selector import StrategySelection, StrategySelector
from execution.paper import PaperExecutionEngine, PaperExecutionRequest
from market.history import HistoricalDataService, PriceBar
from market.session import USStockSessionPolicy
from portfolio.admission import PortfolioAdmissionCoordinator, RiskDecisionStore
from portfolio.allocation import PortfolioAllocator
from portfolio.state import PortfolioState
from research.coordinator import ContinuousResearchCoordinator
from research.performance import StrategyPerformanceStore
from research.scheduler import ContinuousResearchScheduler
from risk.risk_manager import RiskManager
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
    """Evaluates dynamic universe candidates and can hand approved setups to guarded Paper execution."""

    def __init__(
        self,
        ib,
        market_intelligence,
        minimum_signal_score: float = 55.0,
        max_candidates: int = 12,
        quote_budget: int = 40,
        research_budget: int = 2,
        risk_manager: RiskManager | None = None,
        paper_executor: PaperExecutionEngine | None = None,
    ) -> None:
        self.ib = ib
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.performance_store = StrategyPerformanceStore()
        self.selector = StrategySelector(minimum_signal_score, self.performance_store)
        self.research = ContinuousResearchCoordinator(self.history, self.performance_store)
        self.research_scheduler = ContinuousResearchScheduler(
            self.research, self.performance_store, max_attempts_per_cycle=research_budget
        )
        self.session_policy = USStockSessionPolicy()
        self.risk_manager = risk_manager
        risk_pct = 0.01 if risk_manager is None else min(0.01, risk_manager.settings.max_trade_risk_pct)
        max_position_pct = 0.10 if risk_manager is None else risk_manager.settings.max_position_pct
        self.allocator = PortfolioAllocator(risk_pct=risk_pct, max_position_pct=max_position_pct)
        self.admission = None if risk_manager is None else PortfolioAdmissionCoordinator(risk_manager)
        self.risk_decisions = RiskDecisionStore()
        self.paper_executor = paper_executor
        self.pairs = PairsTradingStrategy()
        self.max_candidates = max_candidates
        self.quote_budget = quote_budget
        self.research_budget = max(0, min(research_budget, max_candidates))

    @staticmethod
    def _return_series(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
        closes = [float(b.close) for b in bars[-(lookback + 1):] if float(b.close) > 0]
        if len(closes) < lookback + 1:
            return ()
        return tuple((closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes)))

    @staticmethod
    def _series_correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
        n = min(len(a), len(b))
        if n < 40:
            return None
        x, y = a[-n:], b[-n:]
        mx, my = sum(x) / n, sum(y) / n
        vx = sum((v - mx) ** 2 for v in x)
        vy = sum((v - my) ** 2 for v in y)
        if vx <= 0 or vy <= 0:
            return None
        cov = sum((i - mx) * (j - my) for i, j in zip(x, y))
        return cov / sqrt(vx * vy)

    async def _position_returns(self, state: PortfolioState) -> dict[str, tuple[float, ...]]:
        returns: dict[str, tuple[float, ...]] = {}
        for position in state.positions:
            if position.con_id <= 0:
                continue
            try:
                contract = Contract(
                    conId=position.con_id,
                    symbol=position.symbol,
                    secType=position.asset_class,
                    exchange=position.exchange or "SMART",
                    currency=position.currency or "USD",
                )
                bars = await self.history.bars(contract)
                series = self._return_series(bars)
                if series:
                    returns[position.symbol] = series
            except Exception as exc:
                logger.warning("POSITION HISTORY unavailable | symbol=%s error=%s", position.symbol, exc)
        return returns

    @classmethod
    def _portfolio_correlation(
        cls,
        candidate_returns: tuple[float, ...],
        state: PortfolioState,
        position_returns: dict[str, tuple[float, ...]],
    ) -> float | None:
        if not state.positions:
            return 0.0
        correlations: list[float] = []
        for position in state.positions:
            series = position_returns.get(position.symbol)
            if not series:
                return None
            corr = cls._series_correlation(candidate_returns, series)
            if corr is None:
                return None
            correlations.append(abs(corr))
        return max(correlations) if correlations else None

    async def run_once(
        self,
        rows_per_scanner: int = 25,
        *,
        portfolio_state: PortfolioState | None = None,
    ) -> list[ShadowDecision]:
        ranked = await self.intelligence.ranked_us_opportunity_universe(
            rows_per_plan=rows_per_scanner, quote_budget=self.quote_budget,
        )
        candidates = [x for x in ranked if x.eligible][:self.max_candidates]
        logger.info("SHADOW FUNNEL | ranked=%s eligible=%s deep_analysis=%s",
                    len(ranked), sum(1 for x in ranked if x.eligible), len(candidates))
        decisions: list[ShadowDecision] = []
        history_by_symbol: dict[str, list[PriceBar]] = {}
        position_returns = {} if portfolio_state is None else await self._position_returns(portfolio_state)

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
                candidate_returns = self._return_series(bars)
                asset_class = candidate.contract.secType or "STK"
                selection = self.selector.evaluate(
                    bars, symbol=candidate.symbol,
                    asset_class=asset_class, timeframe="1 hour",
                )
                selected = selection.selected
                action = "NO_TRADE" if selected is None else f"WOULD_{selected.signal.side.value}"
                portfolio_reason = None

                if selected is not None and portfolio_state is not None:
                    signal = selected.signal
                    if signal.entry is None or signal.stop is None:
                        action = "NO_TRADE"
                        portfolio_reason = "invalid_signal_prices"
                    elif self.admission is None:
                        action = "PORTFOLIO_REJECTED"
                        portfolio_reason = "hard_risk_manager_unavailable"
                    else:
                        stress = max(1.0, float(selection.regime.volatility_stress or 1.0))
                        volatility_multiplier = max(0.25, min(1.0, 1.0 / stress))
                        proposal = self.allocator.propose(
                            portfolio_state.snapshot,
                            symbol=candidate.symbol,
                            asset_class=asset_class,
                            entry_price=float(signal.entry),
                            stop_price=float(signal.stop),
                            volatility_multiplier=volatility_multiplier,
                        )
                        if proposal is None:
                            action = "PORTFOLIO_REJECTED"
                            portfolio_reason = "allocation_unavailable"
                        else:
                            corr = self._portfolio_correlation(candidate_returns, portfolio_state, position_returns)
                            admission = self.admission.evaluate(
                                state=portfolio_state,
                                symbol=candidate.symbol,
                                asset_class=asset_class,
                                side=selected.signal.side.value,
                                quantity=proposal.quantity,
                                entry_price=proposal.entry_price,
                                stop_price=proposal.stop_price,
                                proposed_notional=proposal.proposed_notional,
                                proposed_risk=proposal.proposed_risk,
                                candidate_returns=candidate_returns,
                                position_returns=position_returns,
                                correlation_to_portfolio=corr,
                            )
                            portfolio_reason = admission.reason
                            if not admission.approved:
                                action = "PORTFOLIO_REJECTED"
                            else:
                                action = f"APPROVED_{selected.signal.side.value}"
                                if self.paper_executor is not None:
                                    target = signal.target
                                    if target is None or target <= 0:
                                        action = "PAPER_REJECTED"
                                        portfolio_reason = "invalid_target_price"
                                    else:
                                        result = self.paper_executor.submit(
                                            candidate.contract,
                                            PaperExecutionRequest(
                                                symbol=candidate.symbol,
                                                strategy=selected.strategy,
                                                side=selected.signal.side.value,
                                                quantity=proposal.quantity,
                                                entry_price=proposal.entry_price,
                                                stop_price=proposal.stop_price,
                                                target_price=float(target),
                                                regime=selection.regime.regime.value,
                                            ),
                                        )
                                        action = "PAPER_SUBMITTED" if result.submitted else "PAPER_BLOCKED"
                                        portfolio_reason = result.reason
                            self.risk_decisions.record(
                                symbol=candidate.symbol,
                                side=selected.signal.side.value,
                                strategy=selected.strategy,
                                approved=admission.approved,
                                reason=admission.reason,
                                quantity=proposal.quantity,
                                entry_price=proposal.entry_price,
                                stop_price=proposal.stop_price,
                                proposed_notional=proposal.proposed_notional,
                                proposed_risk=proposal.proposed_risk,
                                correlation=corr,
                                regime=selection.regime.regime.value,
                            )
                            logger.info(
                                "PORTFOLIO ADMISSION | symbol=%s qty=%.4f notional=%.2f risk=%.2f vol_mult=%.2f corr=%s approved=%s reason=%s action=%s",
                                candidate.symbol, proposal.quantity, proposal.proposed_notional,
                                proposal.proposed_risk, proposal.volatility_multiplier,
                                "NA" if corr is None else f"{corr:.2f}", admission.approved,
                                admission.reason, action,
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
                    "SHADOW DECISION | symbol=%s liquidity=%.2f regime=%s adx=%.1f ema_slope=%.2f%% vol_stress=%.2f action=%s selected=%s top=%s reason=%s portfolio_reason=%s",
                    candidate.symbol, candidate.score, selection.regime.regime.value,
                    selection.regime.adx, selection.regime.ema_slope_pct,
                    selection.regime.volatility_stress, action,
                    None if selected is None else selected.strategy, top, selection.reason,
                    portfolio_reason,
                )
            except Exception as exc:
                logger.warning("SHADOW candidate skipped | symbol=%s error=%s", candidate.symbol, exc)

        self._evaluate_pairs(history_by_symbol)
        return decisions

    @classmethod
    def _pair_correlation(cls, a: list[PriceBar], b: list[PriceBar]) -> float | None:
        return cls._series_correlation(cls._return_series(a), cls._return_series(b))

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
        logger.info("SHADOW PAIR FUNNEL | combinations=%s relationship_eligible=%s",
                    considered, relationship_eligible)
        return results
