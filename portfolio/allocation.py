from __future__ import annotations

from dataclasses import dataclass

from portfolio.brain import PortfolioOpportunity, PortfolioSnapshot


@dataclass(frozen=True)
class AllocationProposal:
    symbol: str
    asset_class: str
    quantity: float
    entry_price: float
    stop_price: float
    proposed_notional: float
    proposed_risk: float
    volatility_multiplier: float = 1.0

    def as_opportunity(self, correlation_to_portfolio: float = 0.0) -> PortfolioOpportunity:
        return PortfolioOpportunity(
            symbol=self.symbol,
            asset_class=self.asset_class,
            proposed_notional=self.proposed_notional,
            proposed_risk=self.proposed_risk,
            correlation_to_portfolio=correlation_to_portfolio,
        )


class PortfolioAllocator:
    """ATR/stop-distance sizing bounded by hard risk and notional caps."""

    def __init__(self, *, risk_pct: float = 0.01, max_position_pct: float = 0.10) -> None:
        if not 0 < risk_pct <= 1:
            raise ValueError("risk_pct must be between 0 and 1")
        if not 0 < max_position_pct <= 1:
            raise ValueError("max_position_pct must be between 0 and 1")
        self.risk_pct = float(risk_pct)
        self.max_position_pct = float(max_position_pct)

    def propose(
        self,
        snapshot: PortfolioSnapshot,
        *,
        symbol: str,
        asset_class: str,
        entry_price: float,
        stop_price: float,
        volatility_multiplier: float = 1.0,
    ) -> AllocationProposal | None:
        if snapshot.trading_locked or snapshot.net_liquidation <= 0:
            return None
        if entry_price <= 0 or stop_price <= 0:
            return None
        per_unit_risk = abs(entry_price - stop_price)
        if per_unit_risk <= 0:
            return None

        # Higher volatility can only reduce size; it can never grant more risk.
        vol_mult = max(0.10, min(float(volatility_multiplier), 1.0))
        risk_budget = min(
            snapshot.net_liquidation * self.risk_pct * vol_mult,
            snapshot.remaining_daily_loss_budget,
        )
        notional_budget = snapshot.net_liquidation * self.max_position_pct
        if risk_budget <= 0 or notional_budget <= 0:
            return None

        qty_by_risk = risk_budget / per_unit_risk
        qty_by_notional = notional_budget / entry_price
        quantity = max(0.0, min(qty_by_risk, qty_by_notional))
        if asset_class.upper() in {"STK", "ETF", "OPT", "FUT"}:
            quantity = float(int(quantity))
        if quantity <= 0:
            return None

        return AllocationProposal(
            symbol=symbol,
            asset_class=asset_class,
            quantity=quantity,
            entry_price=entry_price,
            stop_price=stop_price,
            proposed_notional=entry_price * quantity,
            proposed_risk=per_unit_risk * quantity,
            volatility_multiplier=vol_mult,
        )
