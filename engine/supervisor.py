from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from config.config import STATE_DIR, BotConfig
from core.account import AccountService, AccountSnapshot
from core.broker_state import BrokerStateService
from core.paper_guard import PaperOrderGuard, build_paper_guard, mask_account
from risk.daily_guard import DailyLossGuard
from risk.drawdown_guard import DrawdownGuard, DrawdownStateStore
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

    def __init__(self, ib, config: BotConfig, state_dir: Path | None = None) -> None:
        self.ib = ib
        self.config = config
        self.accounts = AccountService(ib)
        self.broker = BrokerStateService(ib)
        self.state_dir = Path(state_dir) if state_dir is not None else STATE_DIR
        self.store = DailyRiskStateStore(self.state_dir / "risk_state.json")
        self.context: SupervisorContext | None = None
        self.kill_switch: KillSwitch | None = None
        self.paper_guard: PaperOrderGuard | None = None
        self.drawdown: DrawdownGuard | None = None
        self._allow_drawdown_init = True
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

        # Fail closed: without a verified PAPER session no entry may ever be admitted.
        # Pass only the CONFIGURED account so multiple paper accounts without
        # IBKR_ACCOUNT are refused as ambiguous instead of defaulting to the first.
        self.paper_guard = build_paper_guard(self.ib, self.config.ibkr, self.config.ibkr.account)
        verification = self.paper_guard.verification
        if verification.verified and verification.account != account.account:
            risk.lock_trading("paper_guard_account_differs_from_supervised_account")
        logger.info(
            "PAPER VERIFICATION | verified=%s reason=%s account=%s port=%s connected_port=%s",
            verification.verified, verification.reason, verification.masked_account,
            verification.configured_port, verification.connected_port,
        )
        if not verification.verified and not risk.trading_locked:
            risk.lock_trading(f"paper_account_unverified:{verification.reason}")

        # Multi-day drawdown lock: sticky across days/restarts until a human resets it.
        self.drawdown = DrawdownGuard(DrawdownStateStore(self.state_dir / "drawdown_state.json"),
                                      account.account, self.config.risk.max_drawdown_pct)
        self._allow_drawdown_init = not self.store.has_prior_days(account=account.account,
                                                                  trading_date=trading_date)
        self._check_drawdown(risk, account.net_liquidation)

        guard = DailyLossGuard(risk, persisted.starting_equity)
        self.context = SupervisorContext(
            account=account.account,
            trading_date=trading_date,
            starting_equity=persisted.starting_equity,
            risk=risk,
            guard=guard,
        )
        self.kill_switch = KillSwitch(self.ib, self.config.risk, account.account, guard=self.paper_guard)
        self._kill_persisted = persisted.kill_switch_triggered

        logger.info(
            "SUPERVISOR INIT | account=%s date=%s baseline=%s source=%s sticky_kill=%s",
            mask_account(account.account),
            trading_date,
            "set",
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
        self._roll_trading_day(account.net_liquidation)

        self._check_drawdown(self.context.risk, account.net_liquidation)
        result = self.context.guard.evaluate(account.net_liquidation)
        logger.info(
            "SUPERVISOR RISK | loss_pct=%.2f%% locked=%s",
            result.state.loss_pct * 100,
            result.trading_locked,
        )

        if not result.action_required:
            return None

        # The persisted reason is the daily-loss breach itself, never an earlier unrelated lock.
        reason = (
            f"Daily loss limit reached: {result.state.loss_pct * 100:.2f}% "
            f">= {self.context.risk.settings.max_daily_loss_pct * 100:.2f}%"
        )
        if not self._kill_persisted:
            try:
                self.store.mark_triggered(
                    account=self.context.account,
                    trading_date=self.context.trading_date,
                    reason=reason,
                )
                self._kill_persisted = True
                logger.critical(
                    "SUPERVISOR STICKY KILL persisted | account=%s date=%s",
                    mask_account(self.context.account),
                    self.context.trading_date,
                )
            except Exception as exc:
                # A state-file failure (OneDrive lock, full disk) must never stop the kill switch.
                # Entries are already locked by the daily guard; retried on the next poll.
                logger.critical("SUPERVISOR STICKY KILL NOT PERSISTED | kill switch still runs | %s", exc)
        return await self.kill_switch.execute(reason)

    def _roll_trading_day(self, equity: float, now: datetime | None = None) -> None:
        """A long-running process must measure the DAILY loss from today's baseline, not from the
        day it started. On a new exchange date: new persisted baseline (or the restored one) and a
        new daily guard. Locks already set in memory are NOT released (fail closed: a new day
        never unlocks by itself; a human restarts after reviewing)."""
        today = (now or datetime.now(ZoneInfo("America/New_York"))).date().isoformat()
        if self.context is None or today == self.context.trading_date:
            return
        persisted, created = self.store.load_or_create(
            account=self.context.account, trading_date=today, starting_equity=equity)
        risk = self.context.risk
        if persisted.kill_switch_triggered and not risk.trading_locked:
            risk.lock_trading(persisted.trigger_reason or "sticky daily Kill Switch")
        self.context = replace(self.context, trading_date=today, starting_equity=persisted.starting_equity,
                               guard=DailyLossGuard(risk, persisted.starting_equity))
        self._kill_persisted = persisted.kill_switch_triggered
        self.kill_switch = KillSwitch(self.ib, self.config.risk, self.context.account, guard=self.paper_guard)
        logger.info("SUPERVISOR NEW TRADING DAY | date=%s baseline=%s source=%s locked=%s",
                    today, "set", "CREATED" if created else "RESTORED", risk.trading_locked)

    def _check_drawdown(self, risk: RiskManager, equity: float) -> None:
        """Multi-day drawdown lock. Any failure locks new entries but NEVER prevents the daily
        guard / kill switch from running afterwards."""
        if self.drawdown is None:
            return
        try:
            drawdown = self.drawdown.evaluate(equity, allow_initialize=self._allow_drawdown_init)
        except Exception as exc:
            if not risk.trading_locked:
                risk.lock_trading(f"drawdown guard unavailable: {type(exc).__name__}")
            logger.critical("DRAWDOWN GUARD ERROR | entries locked | %s", exc)
            return
        if drawdown.record.locked and not risk.trading_locked:
            risk.lock_trading(drawdown.record.lock_reason or "multi-day drawdown lock")
            logger.critical("DRAWDOWN LOCK | drawdown=%.2f%% | new entries locked until human reset",
                            drawdown.drawdown_pct * 100)

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
