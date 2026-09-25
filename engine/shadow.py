from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from types import SimpleNamespace
from math import sqrt

from ib_async import Contract

from engine.decision import DecisionPipeline, portfolio_correlation, return_series, series_correlation
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
from research.shadow_journal import ShadowJournal
from risk.risk_manager import RiskManager
from strategies.library import PairSignal, PairsTradingStrategy
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

    def __init__(self, ib, market_intelligence, minimum_signal_score: float = 55.0,
                 max_candidates: int = 12, quote_budget: int = 40, research_budget: int = 2,
                 risk_manager: RiskManager | None = None,
                 paper_executor: PaperExecutionEngine | None = None,
                 session_policy=None) -> None:
        self.ib = ib
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.performance_store = StrategyPerformanceStore()
        self.selector = StrategySelector(minimum_signal_score, self.performance_store)
        self.research = ContinuousResearchCoordinator(self.history, self.performance_store)
        self.research_scheduler = ContinuousResearchScheduler(self.research, self.performance_store,
                                                               max_attempts_per_cycle=research_budget)
        self.session_policy = session_policy or USStockSessionPolicy()
        self.risk_manager = risk_manager
        risk_pct = 0.01 if risk_manager is None else min(0.01, risk_manager.settings.max_trade_risk_pct)
        max_position_pct = 0.10 if risk_manager is None else risk_manager.settings.max_position_pct
        self.allocator = PortfolioAllocator(risk_pct=risk_pct, max_position_pct=max_position_pct)
        self.admission = None if risk_manager is None else PortfolioAdmissionCoordinator(risk_manager)
        self.risk_decisions = RiskDecisionStore()
        try:
            self.journal: ShadowJournal | None = ShadowJournal()
        except Exception as exc:  # research journaling must never stop the shadow engine
            logger.warning("SHADOW JOURNAL unavailable | error=%s", exc)
            self.journal = None
        self.paper_executor = paper_executor
        # The one decision path shared with offline replay (research/pipeline_backtest.py).
        self.decision_pipeline = DecisionPipeline(
            self.selector, self.allocator, self.admission,
            allow_short=bool(getattr(paper_executor, "allow_short", False)),
        )
        self.pairs = PairsTradingStrategy()
        self.max_candidates = max_candidates
        self.quote_budget = quote_budget
        self.research_budget = max(0, min(research_budget, max_candidates))

    async def _fresh_reference(self, candidate) -> tuple[float | None, int | None]:
        """Read-only two-sided quote taken right before a paper submission.

        Returns (mid price, effective IBKR market-data type). The type is the
        WORSE of the configured type and the one the ticker itself reports, so a
        silent fallback to frozen/delayed data is caught. Without a two-sided
        quote the price is None and the executor refuses.
        """
        market_data = getattr(self.intelligence, "market_data", None)
        if market_data is None:
            return None, None
        configured = getattr(getattr(market_data, "settings", None), "market_data_type", None)
        try:
            snapshot = await market_data.snapshot_contract(candidate.contract, candidate.symbol, timeout=3.0)
        except Exception as exc:
            logger.warning("FRESH REFERENCE unavailable | symbol=%s error=%s", candidate.symbol, exc)
            return None, configured
        reported = getattr(snapshot, "market_data_type", None)
        types = [t for t in (configured, reported) if t is not None]
        data_type = max(types) if types else None
        bid, ask = snapshot.bid, snapshot.ask
        if bid and ask and 0 < bid <= ask:
            return (bid + ask) / 2.0, data_type
        return None, data_type

    def _journal_cycle(self, cycle_id, ranked, rows_per_scanner, session) -> None:
        """Point-in-time record of what discovery returned this cycle (never used for decisions)."""
        if self.journal is None:
            return
        try:
            universe = getattr(self.intelligence, "universe", None)
            scanners = [getattr(p, "name", str(p)) for p in getattr(universe, "scanners", ())]
            market_data = getattr(self.intelligence, "market_data", None)
            data_type = getattr(getattr(market_data, "settings", None), "market_data_type", None)
            self.journal.record_cycle(
                cycle_id, universe=getattr(universe, "name", None), scanners=scanners,
                rows_per_scanner=rows_per_scanner, quote_budget=self.quote_budget,
                market_data_type=data_type, session=getattr(session, "session", None),
                market_open=getattr(session, "market_open", None),
            )
            funnel = list(getattr(self.intelligence, "last_funnel", []) or [])
            if funnel:
                self.journal.record_funnel(cycle_id, funnel)
            self.journal.record_discovery(cycle_id, ranked)
        except Exception as exc:  # journaling must never break the loop
            logger.warning("SHADOW JOURNAL cycle write failed | error=%s", exc)

    def _journal_decision(self, cycle_id, candidate, bars, selection, action, reason) -> None:
        if self.journal is None:
            return
        try:
            chosen = selection.selected
            sig = None if chosen is None else chosen.signal
            top_eval = next((e for e in selection.evaluations if e.signal.side.value != "FLAT"), None)
            top = {} if top_eval is None else {
                "strategy": top_eval.strategy, "side": top_eval.signal.side.value,
                "score": float(top_eval.adjusted_score), "entry": top_eval.signal.entry,
                "stop": top_eval.signal.stop, "target": top_eval.signal.target,
            }
            regime = selection.regime
            context = {
                "atr": (regime.atr_pct / 100 * bars[-1].close) if bars else None,
                "adx": regime.adx, "volatility_stress": regime.volatility_stress,
                "liquidity_score": getattr(candidate, "score", None),
                "selector_threshold": getattr(self.selector, "minimum_score", None),
                "reference_close": bars[-1].close if bars else None,
            }
            self.journal.record_decision(
                cycle_id, symbol=candidate.symbol,
                con_id=int(getattr(candidate.contract, "conId", 0) or 0),
                bar_time=bars[-1].time if bars else None, timeframe="1 hour",
                regime=selection.regime.regime.value, action=action,
                strategy=None if chosen is None else chosen.strategy,
                side=None if sig is None else sig.side.value,
                score=None if chosen is None else float(chosen.adjusted_score),
                entry=None if sig is None else sig.entry, stop=None if sig is None else sig.stop,
                target=None if sig is None else sig.target,
                reason=reason or selection.reason, top=top, context=context,
            )
        except Exception as exc:
            logger.warning("SHADOW JOURNAL decision write failed | symbol=%s error=%s", candidate.symbol, exc)

    @staticmethod
    def _return_series(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
        return return_series(bars, lookback)

    @staticmethod
    def _series_correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
        return series_correlation(a, b)

    async def _position_returns(self, state: PortfolioState) -> dict[str, tuple[float, ...]]:
        returns: dict[str, tuple[float, ...]] = {}
        # Working orders count too: the correlation guard fails closed without their series.
        exposures = list(state.positions) + [
            SimpleNamespace(symbol=o.symbol, con_id=o.con_id, asset_class=o.asset_class, exchange="SMART",
                            currency="USD")
            for o in state.pending_orders if o.symbol not in {p.symbol for p in state.positions}
        ]
        for position in exposures:
            if position.con_id <= 0 or position.symbol in returns:
                continue
            try:
                contract = Contract(conId=position.con_id, symbol=position.symbol,
                                    secType=position.asset_class, exchange=position.exchange or "SMART",
                                    currency=position.currency or "USD")
                bars = await self.history.bars(contract)
                series = self._return_series(bars)
                if series:
                    returns[position.symbol] = series
            except Exception as exc:
                logger.warning("POSITION HISTORY unavailable | symbol=%s error=%s", position.symbol, exc)
        return returns

    @classmethod
    def _portfolio_correlation(cls, candidate_returns: tuple[float, ...], state: PortfolioState,
                               position_returns: dict[str, tuple[float, ...]]) -> float | None:
        return portfolio_correlation(candidate_returns, state, position_returns)

    async def run_once(self, rows_per_scanner: int = 25, *,
                       portfolio_state: PortfolioState | None = None) -> list[ShadowDecision]:
        ranked = await self.intelligence.ranked_us_opportunity_universe(
            rows_per_plan=rows_per_scanner, quote_budget=self.quote_budget)
        candidates = [x for x in ranked if x.eligible][:self.max_candidates]
        cycle_id = ShadowJournal.new_cycle_id()
        logger.info("SHADOW FUNNEL | ranked=%s eligible=%s deep_analysis=%s",
                    len(ranked), sum(1 for x in ranked if x.eligible), len(candidates))
        decisions: list[ShadowDecision] = []
        history_by_symbol: dict[str, list[PriceBar]] = {}
        position_returns = {} if portfolio_state is None else await self._position_returns(portfolio_state)

        refresh = getattr(self.session_policy, "refresh", None)
        if refresh is not None:
            await refresh(self.ib)  # read-only broker calendar; failure keeps the market CLOSED
        session = self.session_policy.state()
        self._journal_cycle(cycle_id, ranked, rows_per_scanner, session)
        logger.info("MARKET SESSION | asset=US_STOCKS session=%s market_open=%s local=%s",
                    session.session, session.market_open, session.local_time.isoformat())
        await self.research_scheduler.run_cycle(candidates, market_open=session.market_open)

        for candidate in candidates:
            try:
                bars = await self.history.bars(candidate.contract, complete_only=True)
                history_by_symbol[candidate.symbol] = bars
                asset_class = candidate.contract.secType or "STK"
                decision = self.decision_pipeline.decide(
                    bars, symbol=candidate.symbol, asset_class=asset_class, timeframe="1 hour",
                    portfolio_state=portfolio_state, position_returns=position_returns,
                )
                selection = decision.selection
                selected = selection.selected
                action = decision.action
                portfolio_reason = None if decision.reason == selection.reason else decision.reason
                proposal, admission, corr = decision.proposal, decision.admission, decision.correlation

                if decision.approved and self.paper_executor is not None:
                    signal = selected.signal
                    target = signal.target
                    if target is None or target <= 0:
                        action, portfolio_reason = "PAPER_REJECTED", "invalid_target_price"
                    else:
                        reference_price, data_type = await self._fresh_reference(candidate)
                        result = await self.paper_executor.submit(
                            candidate.contract,
                            PaperExecutionRequest(
                                symbol=candidate.symbol, strategy=selected.strategy,
                                side=signal.side.value, quantity=proposal.quantity,
                                entry_price=proposal.entry_price, stop_price=proposal.stop_price,
                                target_price=float(target), regime=selection.regime.regime.value,
                                account_equity=float(portfolio_state.snapshot.net_liquidation),
                                reference_price=reference_price, market_data_type=data_type,
                                con_id=int(getattr(candidate.contract, "conId", 0) or 0)))
                        action = "PAPER_SUBMITTED" if result.submitted else "PAPER_BLOCKED"
                        portfolio_reason = result.reason
                if admission is not None and proposal is not None:
                    self.risk_decisions.record(
                        symbol=candidate.symbol, side=selected.signal.side.value, strategy=selected.strategy,
                        approved=admission.approved, reason=admission.reason,
                        quantity=proposal.quantity, entry_price=proposal.entry_price,
                        stop_price=proposal.stop_price, proposed_notional=proposal.proposed_notional,
                        proposed_risk=proposal.proposed_risk, correlation=corr,
                        regime=selection.regime.regime.value)
                    logger.info("PORTFOLIO ADMISSION | symbol=%s qty=%.4f notional=%.2f risk=%.2f vol_mult=%.2f corr=%s approved=%s reason=%s action=%s",
                                candidate.symbol, proposal.quantity, proposal.proposed_notional,
                                proposal.proposed_risk, proposal.volatility_multiplier,
                                "NA" if corr is None else f"{corr:.2f}", admission.approved,
                                admission.reason, action)

                decisions.append(ShadowDecision(candidate.symbol, candidate.score, selection, action, portfolio_reason))
                self._journal_decision(cycle_id, candidate, bars, selection, action, portfolio_reason)
                logger.info(
                    "SHADOW DECISION | symbol=%s liquidity=%.2f regime=%s adx=%.1f ema_slope=%.2f%% vol_stress=%.2f action=%s selected=%s top=%s reason=%s portfolio_reason=%s",
                    candidate.symbol, candidate.score, selection.regime.regime.value,
                    selection.regime.adx, selection.regime.ema_slope_pct,
                    selection.regime.volatility_stress, action,
                    None if selected is None else selected.strategy,
                    [f"{x.strategy}:{x.signal.side.value}:{x.adjusted_score:.1f}:learn={x.learning.status.value if x.learning else 'NA'}:{x.learning.confidence if x.learning else 0:.0%}:bonus={x.learning.selector_bonus if x.learning else 0:+.1f}" for x in selection.evaluations[:3]],
                    selection.reason, portfolio_reason)
            except Exception as exc:
                logger.exception("SHADOW candidate failed | symbol=%s error=%s", candidate.symbol, exc)

        pair_candidates = [x for x in candidates if x.symbol in history_by_symbol]
        pair_decisions: list[ShadowPairDecision] = []
        for a, b in combinations(pair_candidates, 2):
            signal = self.pairs.evaluate_pair(history_by_symbol[a.symbol], history_by_symbol[b.symbol])
            if signal.side_a.value != "FLAT" or signal.side_b.value != "FLAT":
                pair_decisions.append(ShadowPairDecision(a.symbol, b.symbol, signal,
                                                         f"{signal.side_a.value}/{signal.side_b.value}"))
        logger.info("SHADOW PAIR FUNNEL | combinations=%s relationship_eligible=%s",
                    len(pair_candidates) * (len(pair_candidates) - 1) // 2, len(pair_decisions))
        return decisions
