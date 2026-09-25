from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from math import sqrt

from ib_async import Contract

from engine.decision import (DECISION_HISTORY_DURATION, DecisionPipeline, decision_context,
                             portfolio_correlation, return_series, series_correlation)
from engine.strategy_selector import StrategySelection, StrategySelector
from execution import pretrade
from execution.paper import PaperExecutionEngine, PaperExecutionRequest
from market.history import HistoricalDataService, PriceBar, bar_size_seconds
from market.session import USStockSessionPolicy
from portfolio.admission import PortfolioAdmissionCoordinator, RiskDecisionStore
from portfolio.allocation import PortfolioAllocator
from portfolio.metadata import ContractMetadataService
from portfolio.state import PendingOrderExposure, PortfolioState
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
                 session_policy=None, state_dir: Path | None = None, *,
                 learning_enabled: bool = False, research_execution: bool = False,
                 run_mode: str = "runtime", max_entries_per_day: int = 3,
                 config_hash: str | None = None) -> None:
        db = None if state_dir is None else Path(state_dir) / "strategy_performance.db"
        if db is not None:
            db.parent.mkdir(parents=True, exist_ok=True)
        self.ib = ib
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.performance_store = StrategyPerformanceStore(db)
        # Learning is OFF by default in every mode (runtime, paper, shadow). Its evidence is not
        # yet valid for selection: regime labels come from a different context length, recent
        # research windows overlap the protocol HOLDOUT, and the status/bonus thresholds have
        # no statistical justification (docs/ARCHITECTURE_TARGET.md §Evidence). With learning
        # disabled the selector gets no performance store (no bonus, no AVOID) and the research
        # scheduler never runs. Enabling it requires its own pre-registered forward test.
        self.learning_enabled = bool(learning_enabled)
        self.selector = StrategySelector(minimum_signal_score,
                                         self.performance_store if self.learning_enabled else None)
        self.research = ContinuousResearchCoordinator(self.history, self.performance_store)
        self.research_scheduler = ContinuousResearchScheduler(self.research, self.performance_store,
                                                               max_attempts_per_cycle=research_budget)
        self.session_policy = session_policy or USStockSessionPolicy()
        self.risk_manager = risk_manager
        risk_pct = 0.01 if risk_manager is None else min(0.01, risk_manager.settings.max_trade_risk_pct)
        max_position_pct = 0.10 if risk_manager is None else risk_manager.settings.max_position_pct
        self.allocator = PortfolioAllocator(risk_pct=risk_pct, max_position_pct=max_position_pct)
        # Runtime admission requires sector metadata (fails closed without it).
        self.admission = None if risk_manager is None else PortfolioAdmissionCoordinator(
            risk_manager, require_sector_metadata=True)
        self.metadata = ContractMetadataService(ib)
        self.risk_decisions = RiskDecisionStore(db)
        try:
            self.journal: ShadowJournal | None = ShadowJournal(db)
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
        # Order-free execution model (shadow-only): same pure pre-trade + hard-risk checks as
        # the executor, a fresh read-only quote, and an in-memory book of today's would-be
        # entries so later candidates/cycles see them as pending exposure (runtime parity).
        self.research_execution = bool(research_execution) and paper_executor is None
        self.run_mode = str(run_mode)
        self.config_hash = config_hash
        self.max_entries_per_day = max(0, int(max_entries_per_day))
        self._virtual_book: list[tuple[str, PendingOrderExposure]] = []
        self._virtual_book_restored = False

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
                market_open=getattr(session, "market_open", None), run_mode=self.run_mode,
            )
            funnel = list(getattr(self.intelligence, "last_funnel", []) or [])
            if funnel:
                self.journal.record_funnel(cycle_id, funnel)
            self.journal.record_discovery(cycle_id, ranked)
        except Exception as exc:  # journaling must never break the loop
            logger.warning("SHADOW JOURNAL cycle write failed | error=%s", exc)

    def _journal_decision(self, cycle_id, candidate, bars, selection, action, reason, extra=None,
                          opportunities=()) -> None:
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
                "run_mode": self.run_mode,
                "learning_mode": "live" if self.learning_enabled else "frozen",
                "selector_bonus": None if top_eval is None else float(top_eval.evidence_bonus),
                "bar_count": len(bars),
                "first_bar_time": str(bars[0].time) if bars else None,
                "input_hash": self._input_hash(bars),
                **(extra or {}),
            }
            decision_id = self.journal.record_decision(
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
            return
        try:
            if opportunities:
                self.journal.record_opportunities(decision_id, cycle_id, opportunities)
        except Exception as exc:  # exploratory records must never affect the decision journal
            logger.warning("SHADOW JOURNAL opportunity write failed | symbol=%s error=%s", candidate.symbol, exc)

    def _journal_error(self, cycle_id, candidate, exc) -> None:
        """A candidate that failed is recorded (it must not silently vanish from the funnel)."""
        if self.journal is None:
            return
        try:
            self.journal.record_decision(
                cycle_id, symbol=candidate.symbol, con_id=int(getattr(candidate.contract, "conId", 0) or 0),
                bar_time=None, timeframe="1 hour", regime="UNKNOWN", action="CANDIDATE_ERROR",
                strategy=None, side=None, score=None, entry=None, stop=None, target=None,
                reason=type(exc).__name__, context={"run_mode": self.run_mode,
                                                    "learning_mode": "live" if self.learning_enabled else "frozen"})
        except Exception as journal_exc:
            logger.warning("SHADOW JOURNAL error write failed | symbol=%s error=%s", candidate.symbol, journal_exc)

    def _journal_cycle_end(self, cycle_id, *, eligible: int, attempted: int, errors: int) -> None:
        if self.journal is None:
            return
        try:
            discovery = getattr(self.intelligence, "discovery", None)
            self.journal.record_cycle_end(cycle_id, eligible=eligible, attempted=attempted, errors=errors,
                                          max_candidates=self.max_candidates, run_mode=self.run_mode,
                                          learning_mode="live" if self.learning_enabled else "frozen",
                                          scanner_rows=getattr(discovery, "last_scan_rows", None),
                                          scanner_errors=getattr(discovery, "last_scan_errors", None),
                                          config_hash=self.config_hash)
        except Exception as exc:
            logger.warning("SHADOW JOURNAL cycle end write failed | error=%s", exc)

    @staticmethod
    def _return_series(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
        return return_series(bars, lookback)

    @staticmethod
    def _series_correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
        return series_correlation(a, b)

    @staticmethod
    def _with_pending_entry(state: PortfolioState, candidate, proposal) -> PortfolioState:
        pending = PendingOrderExposure(
            symbol=candidate.symbol, asset_class=str(getattr(candidate.contract, "secType", "") or "STK"),
            quantity=float(proposal.quantity), reference_price=float(proposal.entry_price),
            notional=float(proposal.proposed_notional),
            con_id=int(getattr(candidate.contract, "conId", 0) or 0),
        )
        snapshot = replace(state.snapshot,
                           pending_order_notional=state.snapshot.pending_order_notional + pending.notional)
        return PortfolioState(snapshot, state.positions, state.pending_orders + (pending,))

    def _with_virtual_book(self, state: PortfolioState | None) -> PortfolioState | None:
        """Today's would-be entries stay pending exposure for the rest of the trading day (a DAY
        limit order works until the close). Conservative: they occupy capacity even if the
        real order would have filled and exited; they are dropped at the next UTC date."""
        today = datetime.now(timezone.utc).date().isoformat()
        self._virtual_book = [(d, o) for d, o in self._virtual_book if d == today]
        if state is None or not self._virtual_book:
            return state
        held = {p.symbol for p in state.positions} | {o.symbol for o in state.pending_orders}
        extra = tuple(o for _, o in self._virtual_book if o.symbol not in held)
        if not extra:
            return state
        snapshot = replace(state.snapshot, pending_order_notional=state.snapshot.pending_order_notional
                           + sum(o.notional for o in extra))
        return PortfolioState(snapshot, state.positions, state.pending_orders + extra)

    def _restore_virtual_book(self) -> None:
        """After a restart, today's would-be entries come back from the journal: a restart must
        not free capacity or reset the daily cap (REPLAY_PARITY N1)."""
        if self.journal is None:
            self._virtual_book_restored = True
            return
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            rows = self.journal.would_be_entries_on(today, self.run_mode)
        except Exception as exc:
            logger.warning("VIRTUAL BOOK restore failed (retried next cycle) | error=%s", exc)
            return
        self._virtual_book_restored = True
        known = {o.symbol for _, o in self._virtual_book}
        for r in rows:
            if r["symbol"] in known or not r["quantity"] or not r["notional"]:
                continue
            self._virtual_book.append((today, PendingOrderExposure(
                symbol=r["symbol"], asset_class="STK", quantity=float(r["quantity"]),
                reference_price=float(r["entry"] or 0.0), notional=float(r["notional"]),
                con_id=int(r["con_id"] or 0))))
            known.add(r["symbol"])

    def _virtual_entries_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(1 for d, _ in self._virtual_book if d == today)

    @staticmethod
    def _input_hash(bars) -> str | None:
        """Fingerprint of the exact bars the selector saw (for determinism checks)."""
        if not bars:
            return None
        import hashlib

        text = "|".join(f"{b.time}:{b.open}:{b.high}:{b.low}:{b.close}:{b.volume}" for b in bars)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _bar_completed_at(bars) -> datetime | None:
        """Completion time of the decision bar (start + 1 hour), tz-aware; None if unknown."""
        if not bars or not isinstance(bars[-1].time, datetime) or bars[-1].time.tzinfo is None:
            return None
        return bars[-1].time + timedelta(seconds=bar_size_seconds("1 hour"))

    async def _research_submission(self, candidate, selected, proposal, state: PortfolioState, session, bars=()):
        """Everything the executor would check before transmitting, minus the broker-only
        checks (paper guard, connection, broker equity). Returns (action, reason, extra)."""
        signal = selected.signal
        target = signal.target
        con_id = int(getattr(candidate.contract, "conId", 0) or 0)
        extra: dict = {}
        if target is None or target <= 0:
            return "SHADOW_BLOCKED", "invalid_target_price", extra
        reference_price, data_type = await self._fresh_reference(candidate)
        extra.update(reference_price=reference_price, reference_data_type=data_type)
        sec_type = str(getattr(candidate.contract, "secType", "") or "")
        check = pretrade.evaluate(
            pretrade.PreTradeRequest(
                side=signal.side.value, quantity=float(proposal.quantity),
                entry_price=float(proposal.entry_price), stop_price=float(proposal.stop_price),
                target_price=float(target), sec_type=sec_type, reference_price=reference_price,
                market_data_type=data_type, bar_completed_at=self._bar_completed_at(bars)),
            pretrade.PreTradeContext(
                now=datetime.now(timezone.utc),
                session_open=bool(getattr(session, "market_open", False)) and sec_type.upper() == "STK",
                trading_locked=bool(getattr(self.risk_manager, "trading_locked", True)),
                entries_today=self._virtual_entries_today(),
                max_entries_per_day=self.max_entries_per_day,
                allow_short=self.decision_pipeline.allow_short),
        )
        if not check.passed:
            return "SHADOW_BLOCKED", check.reason, extra
        refusal = pretrade.hard_risk_refusal(
            self.risk_manager, equity=float(state.snapshot.net_liquidation),
            entry_price=check.request.entry_price, stop_price=check.request.stop_price,
            quantity=check.request.quantity)
        if refusal:
            return "SHADOW_BLOCKED", refusal, extra
        held = [x for x in list(state.positions) + list(state.pending_orders)
                if x.symbol.upper() == candidate.symbol.upper() or (con_id > 0 and int(x.con_id or 0) == con_id)]
        if held:
            return "SHADOW_BLOCKED", "duplicate_symbol_exposure", extra
        return "SHADOW_SUBMIT", "all_pre_trade_checks_passed", extra

    async def _sectors(self, candidate, state: PortfolioState) -> tuple[str | None, dict[str, str]]:
        """Read-only sector lookup for the candidate and every current exposure (cached)."""
        candidate_meta = await self.metadata.get(candidate.contract)
        sectors: dict[str, str] = {}
        exposures = list(state.positions) + list(state.pending_orders)
        for item in exposures:
            con_id = int(getattr(item, "con_id", 0) or 0)
            if con_id <= 0 or item.symbol in sectors:
                continue
            meta = await self.metadata.get(Contract(conId=con_id, exchange="SMART"))
            if meta is not None and meta.known:
                sectors[item.symbol] = meta.sector
        return (candidate_meta.sector if candidate_meta is not None and candidate_meta.known else None), sectors

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
                bars = decision_context(await self.history.bars(
                    contract, duration=DECISION_HISTORY_DURATION, complete_only=True))
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
        if self.research_execution:
            if not self._virtual_book_restored:
                self._restore_virtual_book()
            portfolio_state = self._with_virtual_book(portfolio_state)
        position_returns = {} if portfolio_state is None else await self._position_returns(portfolio_state)

        refresh = getattr(self.session_policy, "refresh", None)
        if refresh is not None:
            await refresh(self.ib)  # read-only broker calendar; failure keeps the market CLOSED
        session = self.session_policy.state()
        self._journal_cycle(cycle_id, ranked, rows_per_scanner, session)
        logger.info("MARKET SESSION | asset=US_STOCKS session=%s market_open=%s local=%s",
                    session.session, session.market_open, session.local_time.isoformat())
        if self.learning_enabled:
            await self.research_scheduler.run_cycle(candidates, market_open=session.market_open)
        attempted = errors = 0

        for candidate in candidates:
            attempted += 1
            try:
                bars = decision_context(await self.history.bars(
                    candidate.contract, duration=DECISION_HISTORY_DURATION, complete_only=True))
                history_by_symbol[candidate.symbol] = bars
                asset_class = candidate.contract.secType or "STK"
                sector, sectors = None, {}
                if portfolio_state is not None:
                    sector, sectors = await self._sectors(candidate, portfolio_state)
                decision = self.decision_pipeline.decide(
                    bars, symbol=candidate.symbol, asset_class=asset_class, timeframe="1 hour",
                    portfolio_state=portfolio_state, position_returns=position_returns,
                    sector=sector, sectors=sectors,
                )
                selection = decision.selection
                selected = selection.selected
                action = decision.action
                portfolio_reason = None if decision.reason == selection.reason else decision.reason
                proposal, admission, corr = decision.proposal, decision.admission, decision.correlation
                stock_type = None
                try:
                    meta = await self.metadata.get(candidate.contract)   # read-only, cached
                    stock_type = None if meta is None else meta.stock_type
                except Exception:
                    stock_type = None
                extra = {"session_open": getattr(session, "market_open", None), "sector": sector,
                         "correlation": corr, "stock_type": stock_type}
                if proposal is not None:
                    extra.update(quantity=proposal.quantity, notional=proposal.proposed_notional,
                                 risk_amount=proposal.proposed_risk,
                                 volatility_multiplier=proposal.volatility_multiplier)

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
                                con_id=int(getattr(candidate.contract, "conId", 0) or 0),
                                bar_completed_at=self._bar_completed_at(bars)))
                        action = "PAPER_SUBMITTED" if result.submitted else "PAPER_BLOCKED"
                        portfolio_reason = result.reason
                        if result.submitted:
                            # Later candidates in this cycle must see this entry as exposure
                            # (gross exposure, correlation, sector), exactly like the replay.
                            portfolio_state = self._with_pending_entry(
                                portfolio_state, candidate, proposal)
                            position_returns = {**position_returns,
                                                candidate.symbol: self._return_series(bars)}
                elif decision.approved and self.research_execution:
                    action, portfolio_reason, execution_extra = await self._research_submission(
                        candidate, selected, proposal, portfolio_state, session, bars)
                    extra.update(execution_extra)
                    if action == "SHADOW_SUBMIT":
                        pending = PendingOrderExposure(
                            symbol=candidate.symbol,
                            asset_class=str(getattr(candidate.contract, "secType", "") or "STK"),
                            quantity=float(proposal.quantity), reference_price=float(proposal.entry_price),
                            notional=float(proposal.proposed_notional),
                            con_id=int(getattr(candidate.contract, "conId", 0) or 0))
                        self._virtual_book.append((datetime.now(timezone.utc).date().isoformat(), pending))
                        portfolio_state = self._with_pending_entry(portfolio_state, candidate, proposal)
                        position_returns = {**position_returns,
                                            candidate.symbol: self._return_series(bars)}
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
                self._journal_decision(cycle_id, candidate, bars, selection, action, portfolio_reason, extra,
                                       opportunities=decision.opportunities)
                logger.info(
                    "SHADOW DECISION | symbol=%s liquidity=%.2f regime=%s adx=%.1f ema_slope=%.2f%% vol_stress=%.2f action=%s selected=%s top=%s reason=%s portfolio_reason=%s",
                    candidate.symbol, candidate.score, selection.regime.regime.value,
                    selection.regime.adx, selection.regime.ema_slope_pct,
                    selection.regime.volatility_stress, action,
                    None if selected is None else selected.strategy,
                    [f"{x.strategy}:{x.signal.side.value}:{x.adjusted_score:.1f}:learn={x.learning.status.value if x.learning else 'NA'}:{x.learning.confidence if x.learning else 0:.0%}:bonus={x.learning.selector_bonus if x.learning else 0:+.1f}" for x in selection.evaluations[:3]],
                    selection.reason, portfolio_reason)
            except Exception as exc:
                errors += 1
                logger.exception("SHADOW candidate failed | symbol=%s error=%s", candidate.symbol, exc)
                self._journal_error(cycle_id, candidate, exc)

        self._journal_cycle_end(cycle_id, eligible=sum(1 for x in ranked if x.eligible),
                                attempted=attempted, errors=errors)

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
