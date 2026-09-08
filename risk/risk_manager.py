from __future__ import annotations

from dataclasses import dataclass

from config.config import RiskConfig
from core.exceptions import RiskRejectedError


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    capital_at_risk: float
    position_value: float
    reason: str = ""


@dataclass(frozen=True)
class DailyRiskState:
    starting_equity: float
    current_equity: float
    pnl: float
    loss_pct: float
    kill_switch_required: bool


class RiskManager:
    """Hard risk layer. Strategies must never bypass this class."""

    def __init__(self, settings: RiskConfig) -> None:
        self.settings = settings
        self.settings.validate()
        self.trading_locked = False
        self.lock_reason = ""

    def lock_trading(self, reason: str) -> None:
        self.trading_locked = True
        self.lock_reason = reason

    def evaluate_trade(
        self,
        *,
        equity: float,
        entry_price: float,
        stop_price: float,
        quantity: float,
    ) -> RiskDecision:
        if self.trading_locked:
            return RiskDecision(False, 0.0, 0.0, f"Trading locked: {self.lock_reason}")

        if equity <= 0 or entry_price <= 0 or stop_price <= 0 or quantity <= 0:
            raise RiskRejectedError("Equity, prices and quantity must be positive")

        position_value = entry_price * quantity
        capital_at_risk = abs(entry_price - stop_price) * quantity
        max_trade_risk = equity * self.settings.max_trade_risk_pct
        max_position_value = equity * self.settings.max_position_pct

        if capital_at_risk > max_trade_risk:
            return RiskDecision(
                False,
                capital_at_risk,
                position_value,
                f"Trade risk {capital_at_risk:.2f} exceeds {max_trade_risk:.2f}",
            )

        if position_value > max_position_value:
            return RiskDecision(
                False,
                capital_at_risk,
                position_value,
                f"Position value {position_value:.2f} exceeds {max_position_value:.2f}",
            )

        return RiskDecision(True, capital_at_risk, position_value, "approved")

    def max_quantity_for_risk(
        self,
        *,
        equity: float,
        entry_price: float,
        stop_price: float,
    ) -> float:
        """Maximum theoretical quantity allowed by the per-trade risk cap.

        Instrument-specific rounding/minimum size is intentionally handled later,
        because stocks, FX, futures and options have different quantity rules.
        """
        if equity <= 0 or entry_price <= 0 or stop_price <= 0:
            raise RiskRejectedError("Equity and prices must be positive")

        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit <= 0:
            raise RiskRejectedError("Stop price must differ from entry price")

        by_risk = (equity * self.settings.max_trade_risk_pct) / risk_per_unit
        by_position = (equity * self.settings.max_position_pct) / entry_price
        return max(0.0, min(by_risk, by_position))

    def daily_state(self, *, starting_equity: float, current_equity: float) -> DailyRiskState:
        if starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")
        if current_equity < 0:
            raise ValueError("current_equity must be >= 0")

        pnl = current_equity - starting_equity
        loss_pct = max(0.0, -pnl / starting_equity)
        triggered = loss_pct >= self.settings.max_daily_loss_pct
        return DailyRiskState(
            starting_equity=starting_equity,
            current_equity=current_equity,
            pnl=pnl,
            loss_pct=loss_pct,
            kill_switch_required=triggered,
        )

    def daily_loss_triggered(self, *, starting_equity: float, current_equity: float) -> bool:
        return self.daily_state(
            starting_equity=starting_equity,
            current_equity=current_equity,
        ).kill_switch_required
