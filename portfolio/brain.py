from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioSnapshot:
    net_liquidation: float
    cash: float
    committed_notional: float
    open_position_risk: float
    pending_order_notional: float
    daily_loss_used: float
    daily_loss_limit: float
    trading_locked: bool = False

    @property
    def remaining_daily_loss_budget(self) -> float:
        return max(0.0, self.daily_loss_limit - self.daily_loss_used)

    @property
    def gross_exposure_pct(self) -> float:
        if self.net_liquidation <= 0:
            return 0.0
        return max(0.0, self.committed_notional) / self.net_liquidation


@dataclass(frozen=True)
class PortfolioOpportunity:
    symbol: str
    asset_class: str
    proposed_notional: float
    proposed_risk: float
    correlation_to_portfolio: float = 0.0


@dataclass(frozen=True)
class PortfolioDecision:
    approved: bool
    reason: str
    projected_exposure_pct: float
    remaining_daily_loss_budget: float


class PortfolioBrain:
    """Portfolio-level admission gate for candidate opportunities.

    This component decides whether a candidate may consume portfolio capacity.
    It never places orders and cannot increase broker permissions or hard risk limits.
    """

    def __init__(
        self,
        *,
        max_gross_exposure_pct: float = 0.80,
        max_single_position_pct: float = 0.25,
        max_correlation: float = 0.85,
        cash_reserve_pct: float = 0.05,
    ) -> None:
        self.max_gross_exposure_pct = max(0.0, min(float(max_gross_exposure_pct), 1.0))
        self.max_single_position_pct = max(0.0, min(float(max_single_position_pct), 1.0))
        self.max_correlation = max(0.0, min(float(max_correlation), 1.0))
        self.cash_reserve_pct = max(0.0, min(float(cash_reserve_pct), 1.0))

    def evaluate(self, snapshot: PortfolioSnapshot, opportunity: PortfolioOpportunity) -> PortfolioDecision:
        if snapshot.trading_locked:
            return PortfolioDecision(False, "trading_locked", snapshot.gross_exposure_pct,
                                     snapshot.remaining_daily_loss_budget)
        if snapshot.net_liquidation <= 0:
            return PortfolioDecision(False, "invalid_net_liquidation", 0.0,
                                     snapshot.remaining_daily_loss_budget)
        if opportunity.proposed_notional <= 0 or opportunity.proposed_risk <= 0:
            return PortfolioDecision(False, "invalid_opportunity_size", snapshot.gross_exposure_pct,
                                     snapshot.remaining_daily_loss_budget)

        single_pct = opportunity.proposed_notional / snapshot.net_liquidation
        if single_pct > self.max_single_position_pct:
            return PortfolioDecision(False, "single_position_limit", snapshot.gross_exposure_pct,
                                     snapshot.remaining_daily_loss_budget)

        projected_notional = (
            snapshot.committed_notional + snapshot.pending_order_notional + opportunity.proposed_notional
        )
        projected_exposure = projected_notional / snapshot.net_liquidation
        if projected_exposure > self.max_gross_exposure_pct:
            return PortfolioDecision(False, "gross_exposure_limit", projected_exposure,
                                     snapshot.remaining_daily_loss_budget)

        minimum_cash_reserve = snapshot.net_liquidation * self.cash_reserve_pct
        projected_cash = snapshot.cash - opportunity.proposed_notional
        if projected_cash < minimum_cash_reserve:
            return PortfolioDecision(False, "cash_reserve_limit", projected_exposure,
                                     snapshot.remaining_daily_loss_budget)

        if opportunity.proposed_risk > snapshot.remaining_daily_loss_budget:
            return PortfolioDecision(False, "daily_loss_budget_exhausted", projected_exposure,
                                     snapshot.remaining_daily_loss_budget)

        if abs(opportunity.correlation_to_portfolio) > self.max_correlation:
            return PortfolioDecision(False, "correlation_limit", projected_exposure,
                                     snapshot.remaining_daily_loss_budget)

        return PortfolioDecision(True, "portfolio_capacity_available", projected_exposure,
                                 snapshot.remaining_daily_loss_budget)
