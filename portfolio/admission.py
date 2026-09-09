from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import sqlite3

from portfolio.brain import PortfolioBrain, PortfolioDecision
from portfolio.exposure import CrossExposureGuard, ExposureDecision, ExposurePosition
from portfolio.state import PortfolioState
from risk.risk_manager import RiskDecision, RiskManager


@dataclass(frozen=True)
class PortfolioAdmissionDecision:
    approved: bool
    reason: str
    hard_risk: RiskDecision
    portfolio: PortfolioDecision | None
    exposure: ExposureDecision | None
    correlation_to_portfolio: float | None


class RiskDecisionStore:
    """Append-only audit trail for portfolio admission decisions."""

    def __init__(self, path: str | Path = "state/strategy_performance.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    proposed_notional REAL NOT NULL,
                    proposed_risk REAL NOT NULL,
                    correlation REAL,
                    regime TEXT,
                    source TEXT NOT NULL DEFAULT 'SHADOW'
                )
                """
            )

    def record(
        self,
        *,
        symbol: str,
        side: str,
        strategy: str,
        approved: bool,
        reason: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        proposed_notional: float,
        proposed_risk: float,
        correlation: float | None,
        regime: str,
        source: str = "SHADOW",
    ) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO risk_decisions (
                    created_at, symbol, side, strategy, approved, reason, quantity,
                    entry_price, stop_price, proposed_notional, proposed_risk,
                    correlation, regime, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), symbol.upper(), side.upper(),
                    strategy, int(bool(approved)), reason, float(quantity), float(entry_price),
                    float(stop_price), float(proposed_notional), float(proposed_risk),
                    None if correlation is None else float(correlation), regime, source.upper(),
                ),
            )
            return int(cur.lastrowid)


class PortfolioAdmissionCoordinator:
    """Combines hard trade risk, portfolio capacity and cross-exposure checks.

    The coordinator has no broker execution authority. Missing correlation information
    fails closed whenever the account already has open positions.
    """

    def __init__(
        self,
        risk: RiskManager,
        *,
        portfolio_brain: PortfolioBrain | None = None,
        exposure_guard: CrossExposureGuard | None = None,
    ) -> None:
        self.risk = risk
        self.portfolio_brain = portfolio_brain or PortfolioBrain(
            max_single_position_pct=risk.settings.max_position_pct
        )
        self.exposure_guard = exposure_guard or CrossExposureGuard()

    def evaluate(
        self,
        *,
        state: PortfolioState,
        symbol: str,
        asset_class: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        proposed_notional: float,
        proposed_risk: float,
        candidate_returns: tuple[float, ...],
        position_returns: dict[str, tuple[float, ...]],
        correlation_to_portfolio: float | None,
        sector: str = "UNKNOWN",
    ) -> PortfolioAdmissionDecision:
        hard = self.risk.evaluate_trade(
            equity=state.snapshot.net_liquidation,
            entry_price=entry_price,
            stop_price=stop_price,
            quantity=quantity,
        )
        if not hard.approved:
            return PortfolioAdmissionDecision(False, f"hard_risk:{hard.reason}", hard, None, None, correlation_to_portfolio)

        if state.positions and correlation_to_portfolio is None:
            return PortfolioAdmissionDecision(False, "correlation_unavailable", hard, None, None, None)

        opportunity = __import__("portfolio.brain", fromlist=["PortfolioOpportunity"]).PortfolioOpportunity(
            symbol=symbol,
            asset_class=asset_class,
            proposed_notional=proposed_notional,
            proposed_risk=proposed_risk,
            correlation_to_portfolio=0.0 if correlation_to_portfolio is None else correlation_to_portfolio,
        )
        portfolio = self.portfolio_brain.evaluate(state.snapshot, opportunity)
        if not portfolio.approved:
            return PortfolioAdmissionDecision(False, portfolio.reason, hard, portfolio, None, correlation_to_portfolio)

        positions = tuple(
            ExposurePosition(
                symbol=p.symbol,
                side="LONG" if p.quantity > 0 else "SHORT",
                notional=p.notional,
                sector="UNKNOWN",
                returns=position_returns.get(p.symbol, ()),
            )
            for p in state.positions
        )
        exposure = self.exposure_guard.evaluate(
            net_liquidation=state.snapshot.net_liquidation,
            positions=positions,
            proposed_symbol=symbol,
            proposed_side=side,
            proposed_notional=proposed_notional,
            proposed_sector=sector,
            proposed_returns=candidate_returns,
        )
        if state.positions and any(not position_returns.get(p.symbol) for p in state.positions):
            return PortfolioAdmissionDecision(False, "position_history_unavailable", hard, portfolio, exposure, correlation_to_portfolio)
        if not exposure.approved:
            return PortfolioAdmissionDecision(False, exposure.reason, hard, portfolio, exposure, correlation_to_portfolio)

        return PortfolioAdmissionDecision(True, "portfolio_risk_approved", hard, portfolio, exposure, correlation_to_portfolio)
