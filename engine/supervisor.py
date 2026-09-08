from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from config.config import BotConfig
from core.account import AccountService, AccountSnapshot
from core.broker_state import BrokerStateService
from risk.daily_guard import DailyLossGuard
from risk.kill_switch import KillSwitch, KillSwitchResult
from risk.risk_manager import RiskManager
from risk.state_store import DailyRiskStateStore
from utils.logger import logger


@dataclass(frozen=True)
class SupervisorContext:
    account: str
    trading_date: str
    starting_equity: float
    risk: RiskManager
    guard: DailyLossGuard


class PaperSupervisor:
    """Long-running safety supervisor for an autonomous Paper session."""

    def __init__(self, ib, config: BotConfig) -> None:
        self.ib = ib
        self.config = config
        self.accounts = AccountService(ib)
        self.broker = BrokerStateService(ib)
        self.store = DailyRiskStateStore()
        self.context: SupervisorContext | None = None
        self.kill_switch: KillSwitch | None = None
        self._kill_persisted = False

    async def initialize(self) -> tuple[SupervisorContext, AccountSnapshot]:
        account = await self.accounts.snapshot(self.config.ibkr.account)
        if account.net_liquidation is None or account.net_liquidation <= 0:
            raise RuntimeError("Cannot start supervisor without valid NetLiquidation")

        trading_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        persisted, created = self.store.load_or_create(
            account=account.account,
            trading_date=trading_date,
            starting_equity=account.net_liquidation,
        )

        risk = RiskManager(self.config.risk)
        if persisted.kill_switch_triggered:
            risk.lock_trading(persisted.trigger_reason or "sticky daily Kill Switch")

        guard = DailyLossGuard(risk, persisted.starting_equity)
        self.context = SupervisorContext(
            account=account.account,
            trading_date=trading_date,
            starting_equity=persisted.starting_equity,
            risk=risk,
            guard=guard,
        )
        self.kill_switch = KillSwitch(self.ib, self.config.risk, account.account)
        self._kill_persisted = persisted.kill_switch_triggered

        logger.info(
            "SUPERVISOR INIT | account=%s date=%s baseline=%.2f source=%s sticky_kill=%s",
            account.account,
            trading_date,
            persisted.starting_equity,
            "CREATED" if created else "RESTORED",
            persisted.kill_switch_triggered,
        )

        broker_state = self.broker.snapshot(account.account)
        if persisted.kill_switch_triggered and not broker_state.is_flat:
            await self.kill_switch.execute(
                persisted.trigger_reason or "sticky daily Kill Switch restored"
            )
        elif self.config.runtime.require_flat_startup and not broker_state.is_flat:
            reason = (
                f"Startup broker state not flat: positions={broker_state.position_count} "
                f"open_orders={broker_state.open_order_count}"
            )
            risk.lock_trading(reason)
            logger.critical("SUPERVISOR STARTUP LOCK | %s", reason)

        await self.evaluate(account)
        return self.context, account

    async def evaluate(self, account: AccountSnapshot | None = None) -> KillSwitchResult | None:
        if self.context is None or self.kill_switch is None:
            raise RuntimeError("Supervisor not initialized")

        account = account or await self.accounts.snapshot(self.context.account, log=False)
        if account.net_liquidation is None:
            raise RuntimeError("Account snapshot missing NetLiquidation")

        result = self.context.guard.evaluate(account.net_liquidation)
        logger.info(
            "SUPERVISOR RISK | baseline=%.2f current=%.2f pnl=%.2f loss_pct=%.2f%% locked=%s",
            result.state.starting_equity,
            result.state.current_equity,
            result.state.pnl,
            result.state.loss_pct * 100,
            result.trading_locked,
        )

        if not result.action_required:
            return None

        reason = self.context.risk.lock_reason or "daily loss limit reached"
        if not self._kill_persisted:
            self.store.mark_triggered(
                account=self.context.account,
                trading_date=self.context.trading_date,
                reason=reason,
            )
            self._kill_persisted = True
            logger.critical(
                "SUPERVISOR STICKY KILL persisted | account=%s date=%s",
                self.context.account,
                self.context.trading_date,
            )
        return await self.kill_switch.execute(reason)

    async def run(self, stop_event: asyncio.Event) -> None:
        if self.context is None:
            await self.initialize()

        while not stop_event.is_set():
            try:
                await self.evaluate()
            except Exception as exc:
                # Fail closed on monitoring uncertainty: do not unlock entries.
                if self.context is not None:
                    self.context.risk.lock_trading(f"Supervisor monitoring failure: {exc}")
                logger.exception("SUPERVISOR LOOP ERROR | trading locked: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.config.runtime.supervisor_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass
