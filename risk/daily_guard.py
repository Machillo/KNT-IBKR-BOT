from __future__ import annotations

from dataclasses import dataclass

from risk.risk_manager import DailyRiskState, RiskManager
from utils.logger import logger


@dataclass(frozen=True)
class DailyGuardResult:
    state: DailyRiskState
    trading_locked: bool
    action_required: bool


class DailyLossGuard:
    """Tracks session equity and locks new entries when the daily loss cap is hit."""

    def __init__(self, risk: RiskManager, starting_equity: float) -> None:
        if starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")
        self.risk = risk
        self.starting_equity = starting_equity

    def evaluate(self, current_equity: float) -> DailyGuardResult:
        state = self.risk.daily_state(
            starting_equity=self.starting_equity,
            current_equity=current_equity,
        )

        if state.kill_switch_required and not self.risk.trading_locked:
            reason = (
                f"Daily loss limit reached: {state.loss_pct * 100:.2f}% "
                f">= {self.risk.settings.max_daily_loss_pct * 100:.2f}%"
            )
            self.risk.lock_trading(reason)
            logger.critical(
                "DAILY LOSS GUARD TRIGGERED | starting_equity=%.2f current_equity=%.2f "
                "pnl=%.2f loss_pct=%.2f%% | new entries locked",
                state.starting_equity,
                state.current_equity,
                state.pnl,
                state.loss_pct * 100,
            )

        return DailyGuardResult(
            state=state,
            trading_locked=self.risk.trading_locked,
            action_required=state.kill_switch_required,
        )
