"""The single decision path shared by runtime shadow/paper and offline replay.

``DecisionPipeline.decide`` turns completed bars + portfolio state into an action:

    selector (regime, strategies, threshold / NO_TRADE)
    → side policy (shorts disabled unless explicitly allowed; executor refuses them too)
    → optional ``filters`` hook (unused today; research filters live in the replay runner)
    → PortfolioAllocator (risk % × volatility multiplier, notional cap, whole shares)
    → PortfolioAdmissionCoordinator (RiskManager, PortfolioBrain, CrossExposureGuard)

It has no broker access and never submits anything. ``engine/shadow.py`` calls it and
then (only when approved and an executor is armed) hands the proposal to the guarded
paper executor; ``research/pipeline_backtest.py`` calls the very same object and then
simulates execution. Keeping one implementation is what prevents "the strategy we
backtest" from drifting away from "the decision KNT would take".
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Callable

from engine.strategy_selector import StrategySelection
from market.history import PriceBar
from portfolio.admission import PortfolioAdmissionDecision
from portfolio.allocation import AllocationProposal
from portfolio.state import PortfolioState
from strategies.momentum import SignalSide

# (side, bars) -> refusal reason or None. Research-only hook.
SignalFilter = Callable[[SignalSide, list[PriceBar]], "str | None"]


@dataclass(frozen=True)
class TradeDecision:
    symbol: str
    action: str   # NO_TRADE | WOULD_LONG/SHORT | PORTFOLIO_REJECTED | APPROVED_LONG/SHORT
    reason: str
    selection: StrategySelection
    proposal: AllocationProposal | None = None
    admission: PortfolioAdmissionDecision | None = None
    correlation: float | None = None

    @property
    def approved(self) -> bool:
        return self.action.startswith("APPROVED_")


def return_series(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
    closes = [float(b.close) for b in bars[-(lookback + 1):] if float(b.close) > 0]
    if len(closes) < lookback + 1:
        return ()
    return tuple((closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes)))


def series_correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
    n = min(len(a), len(b))
    if n < 40:
        return None
    x, y = a[-n:], b[-n:]
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return None
    return sum((i - mx) * (j - my) for i, j in zip(x, y)) / sqrt(vx * vy)


def exposure_symbols(state: PortfolioState) -> list[str]:
    """Symbols already exposed: open positions AND working orders (e.g. entries submitted
    earlier in the same cycle). Both must count for the correlation guard."""
    seen: list[str] = []
    for item in (*state.positions, *state.pending_orders):
        if item.symbol not in seen:
            seen.append(item.symbol)
    return seen


def portfolio_correlation(candidate_returns: tuple[float, ...], state: PortfolioState,
                          position_returns: dict[str, tuple[float, ...]]) -> float | None:
    """Max |correlation| to any open position or working order; None (fail closed) if any
    required series is missing."""
    symbols = exposure_symbols(state)
    if not symbols:
        return 0.0
    correlations: list[float] = []
    for symbol in symbols:
        series = position_returns.get(symbol)
        if not series:
            return None
        corr = series_correlation(candidate_returns, series)
        if corr is None:
            return None
        correlations.append(abs(corr))
    return max(correlations) if correlations else None


class DecisionPipeline:
    def __init__(self, selector, allocator, admission, *, allow_short: bool = False,
                 filters: tuple[SignalFilter, ...] = ()) -> None:
        self.selector = selector
        self.allocator = allocator
        self.admission = admission
        self.allow_short = bool(allow_short)
        self.filters = tuple(filters)

    def select(self, bars: list[PriceBar], *, symbol: str, asset_class: str, timeframe: str) -> StrategySelection:
        return self.selector.evaluate(bars, symbol=symbol, asset_class=asset_class, timeframe=timeframe)

    def decide(self, bars: list[PriceBar], *, symbol: str, asset_class: str = "STK", timeframe: str = "1 hour",
               portfolio_state: PortfolioState | None = None,
               position_returns: dict[str, tuple[float, ...]] | None = None,
               selection: StrategySelection | None = None,
               sector: str | None = None, sectors: dict[str, str] | None = None) -> TradeDecision:
        selection = selection or self.select(bars, symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        chosen = selection.selected
        if chosen is None:
            return TradeDecision(symbol, "NO_TRADE", selection.reason, selection)
        signal = chosen.signal
        if signal.side == SignalSide.SHORT and not self.allow_short:
            return TradeDecision(symbol, "NO_TRADE", "short_entries_disabled", selection)
        for check in self.filters:
            refusal = check(signal.side, bars)
            if refusal:
                return TradeDecision(symbol, "NO_TRADE", refusal, selection)
        if portfolio_state is None:
            return TradeDecision(symbol, f"WOULD_{signal.side.value}", selection.reason, selection)
        if signal.entry is None or signal.stop is None:
            return TradeDecision(symbol, "NO_TRADE", "invalid_signal_prices", selection)
        if self.admission is None:
            return TradeDecision(symbol, "PORTFOLIO_REJECTED", "hard_risk_manager_unavailable", selection)
        stress = max(1.0, float(selection.regime.volatility_stress or 1.0))
        volatility_multiplier = max(0.25, min(1.0, 1.0 / stress))
        proposal = self.allocator.propose(
            portfolio_state.snapshot, symbol=symbol, asset_class=asset_class,
            entry_price=float(signal.entry), stop_price=float(signal.stop),
            volatility_multiplier=volatility_multiplier,
        )
        if proposal is None:
            return TradeDecision(symbol, "PORTFOLIO_REJECTED", "allocation_unavailable", selection)
        position_returns = position_returns or {}
        candidate_returns = return_series(bars)
        corr = portfolio_correlation(candidate_returns, portfolio_state, position_returns)
        admission = self.admission.evaluate(
            state=portfolio_state, symbol=symbol, asset_class=asset_class, side=signal.side.value,
            quantity=proposal.quantity, entry_price=proposal.entry_price, stop_price=proposal.stop_price,
            proposed_notional=proposal.proposed_notional, proposed_risk=proposal.proposed_risk,
            candidate_returns=candidate_returns, position_returns=position_returns,
            correlation_to_portfolio=corr, sector=sector or "UNKNOWN", sectors=sectors or {},
        )
        action = f"APPROVED_{signal.side.value}" if admission.approved else "PORTFOLIO_REJECTED"
        return TradeDecision(symbol, action, admission.reason, selection, proposal, admission, corr)
